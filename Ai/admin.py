"""Independent keyboard administration dashboard, with plain-terminal fallback."""
import argparse
from importlib.util import find_spec
import concurrent.futures
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit
from Ai import tui
from Ai.manager_cli import execute, parser, snapshot, logs

ACTIONS = [('Status', 'status'), ('Start', 'start'), ('Stop', 'stop'), ('Restart', 'restart'),
           ('Check updates', 'update check'), ('Apply cached update', 'update apply --offline'),
           ('Rollback', 'update rollback'), ('Sources', 'source list'), ('Configuration', 'config')]


MODEL_PRESETS = {
    'qwen4': ('Qwen/Qwen3-4B-GGUF', 'Qwen3-4B-Q4_K_M.gguf'),
    'qwen8': ('Qwen/Qwen3-8B-GGUF', 'Qwen3-8B-Q4_K_M.gguf'),
    'gemma2': ('bartowski/gemma-2-2b-it-GGUF', 'gemma-2-2b-it-Q4_K_M.gguf'),
    'gemma4': ('ggml-org/gemma-3-4b-it-GGUF', 'gemma-3-4b-it-Q4_K_M.gguf'),
    'gemma9': ('bartowski/gemma-2-9b-it-GGUF', 'gemma-2-9b-it-Q4_K_M.gguf'),
}
ACTIONS += [
    ('Download Qwen3 4B GGUF (~2.5 GB)', 'model download qwen4'),
    ('Download Qwen3 8B GGUF (~5 GB)', 'model download qwen8'),
    ('Download Gemma 2 2B GGUF (~1.7 GB)', 'model download gemma2'),
    ('Download Gemma 3 4B GGUF (~2.5 GB)', 'model download gemma4'),
    ('Download Gemma 2 9B GGUF (~5.8 GB)', 'model download gemma9'),
    ('Pick installed model', 'model pick'),
    ('Paste Hugging Face GGUF link', 'model download'),
]


def model_reference(value):
    if value in MODEL_PRESETS:
        return (*MODEL_PRESETS[value], None)
    url = urlsplit(value)
    parts = [unquote(part) for part in url.path.split('/')[1:]]
    if (url.scheme != 'https' or url.netloc != 'huggingface.co'
            or url.query or url.fragment or len(parts) < 5
            or parts[2] not in ('blob', 'resolve')
            or any(not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', part) for part in parts)
            or not parts[-1].lower().endswith('.gguf')):
        raise ValueError('Paste a Hugging Face HTTPS link to a GGUF file (blob or resolve).')
    return '/'.join(parts[:2]), '/'.join(parts[4:]), parts[3]


def download_selected_model(value):
    from Ai.local_manager import data_directory, download_model

    repo, filename, revision = model_reference(value)
    directory = data_directory() / 'models' / repo.replace('/', '--')
    path = download_model(argparse.Namespace(
        repo=repo, filename=filename, revision=revision, directory=directory,
    ), announce=False)
    return {'downloaded': str(path), 'next': 'Choose Pick installed model, then Start or Restart.'}


def model_choices():
    from Ai.local_manager import data_directory

    directory = data_directory() / 'models'
    choices = [
        (str(path.relative_to(directory)), 'model select ' + shlex.quote(str(path)))
        for path in sorted(directory.rglob('*.gguf')) if path.is_file()
    ]
    return choices + [('Use configured provider', 'model select'), ('Back', 'back')]


class SetupRequired(Exception):
    def __init__(self, label, command):
        super().__init__(label)
        self.command = command


def check_setup(words, runtime):
    if words[:2] == ['model', 'download'] and len(words) == 3:
        model_reference(words[2])
        if find_spec('huggingface_hub') is None:
            raise SetupRequired('Download and install Hugging Face support?',
                                [sys.executable, '-m', 'pip', 'install', '--only-binary=:all:',
                                 'huggingface_hub>=0.27,<2'])
    if words not in (['start'], ['restart']):
        return
    from Ai.managed_model import runner_executable, selected_model_path
    path = selected_model_path(runtime.root)
    if path is None or not path.is_file() or runner_executable():
        return
    brew = shutil.which('brew') or shutil.which('brew', path='/opt/homebrew/bin:/usr/local/bin')
    if sys.platform != 'darwin' or not brew:
        raise ValueError('Install llama.cpp so llama-server is available, then retry Start.')
    raise SetupRequired('Download and install llama.cpp with Homebrew?', [brew, 'install', 'llama.cpp'])


def install_and_retry(setup, text, runtime, releases):
    log = runtime.root / 'setup.log'
    with log.open('ab') as output:
        result = subprocess.run(setup.command, stdin=subprocess.DEVNULL, stdout=output,
                                stderr=subprocess.STDOUT, timeout=600)
    if result.returncode:
        raise RuntimeError(f'Installation failed. See {log}.')
    return action(text, runtime, releases)


def action(text, runtime, releases):
    words = shlex.split(text)
    check_setup(words, runtime)
    if words[:2] == ['model', 'download']:
        if len(words) != 3:
            raise ValueError('Enter model download followed by a GGUF link or preset.')
        return download_selected_model(words[2])
    if words[:2] == ['model', 'select']:
        from Ai.manager_runtime import FileLock, atomic_json

        with FileLock(runtime.root / 'operation.lock'):
            selection = runtime.root / 'model.json'
            if len(words) == 2:
                selection.unlink(missing_ok=True)
                return {'model': 'configured provider', 'next': 'Start or Restart to apply.'}
            path = Path(words[2]).resolve()
            if len(words) != 3 or not path.is_file() or path.suffix.lower() != '.gguf':
                raise ValueError('Select an existing GGUF file.')
            atomic_json(selection, {'path': str(path)})
            return {'model': str(path), 'next': 'Start or Restart to apply.'}
    args = parser().parse_args(words)
    if args.command == 'remote':
        import subprocess
        from Ai.manager_cli import remote_command
        result = subprocess.run(remote_command(args.host, args.action), capture_output=True, text=True, timeout=600)
        return {'exit_code': result.returncode, 'output': result.stdout, 'diagnostic': result.stderr}
    if args.command == 'admin' or (args.command == 'start' and args.foreground):
        raise ValueError('Use the shell for remote actions and foreground mode.')
    return execute(args, runtime, releases)


def lines_for(state):
    server = state['server']
    return ['Super Bot administration',
            f"Server: {server['state']}   Version: {state['release']['version']}",
            f"Source: {state['source']['kind']} / {state['source']['channel']}",
            f"Clients: {server.get('clients', '-')}   Sessions: {server.get('sessions', '-')}",
            f"Next start model: {Path(state['selected_model']).name if state.get('selected_model') else 'configured provider'}",
            f"Instance: {state['instance']}"]


def run(args, runtime, releases):
    if args.once or not sys.stdin.isatty() or not sys.stdout.isatty():
        print(json.dumps(snapshot(runtime, releases), indent=2))
        return 0
    if args.plain or os.getenv('TERM') == 'dumb':
        return plain(runtime, releases)
    try:
        import curses
    except ImportError:
        return plain(runtime, releases)
    try:
        return curses.wrapper(screen, runtime, releases)
    except curses.error:
        return plain(runtime, releases)


def plain(runtime, releases):
    while True:
        print('\n'.join(lines_for(snapshot(runtime, releases))))
        print(' | '.join(f'{i+1} {label}' for i, (label, _) in enumerate(ACTIONS)))
        print('Enter a number, a manager command (including source use), or q. Remote: use SSH in your shell.')
        try:
            value = input('admin> ').strip()
        except EOFError:
            return 0
        if value in ('q', 'quit'):
            return 0
        if value.isdigit() and 1 <= int(value) <= len(ACTIONS):
            value = ACTIONS[int(value)-1][1]
        try:
            if value == 'model pick':
                choices = model_choices()
                for i, (label, _) in enumerate(choices, 1):
                    print(f'{i}. {label}')
                number = input('Model number (blank to cancel)> ').strip()
                if not number:
                    continue
                if not number.isdecimal() or not 1 <= int(number) <= len(choices):
                    raise ValueError('Choose a model number from the list.')
                value = choices[int(number)-1][1]
                if value == 'back':
                    continue
            if value == 'model download':
                link = input('Hugging Face GGUF URL (blank to cancel)> ').strip()
                if not link:
                    continue
                value += ' ' + shlex.quote(link)
            try:
                result = action(value, runtime, releases)
            except SetupRequired as setup:
                if input(f'{setup} [y/N] ').strip().lower() not in ('y', 'yes'):
                    continue
                result = install_and_retry(setup, value, runtime, releases)
            print(json.dumps(result, indent=2))
        except (Exception, SystemExit) as exc:
            print(f'Action failed: {exc}')


def screen(window, runtime, releases):
    import curses
    window.keypad(True)
    window.timeout(200)
    selected, output, command = 0, [], None
    future = None
    actions = ACTIONS
    pending_setup = None
    submitted = None
    last = 0
    state = snapshot(runtime, releases)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        while True:
            now = time.monotonic()
            if future and future.done():
                try:
                    output = json.dumps(future.result(), indent=2).splitlines()
                except SetupRequired as setup:
                    pending_setup = setup
                    output = [str(setup)]
                except (Exception, SystemExit) as exc:
                    output = [f'Action failed: {exc}']
                future = None
                last = 0
            if now - last > 2 and not future:
                state = snapshot(runtime, releases)
                last = now
            height, width = window.getmaxyx()
            window.erase()
            rows = lines_for(state)
            visible = max(1, height - 10)
            first = max(0, selected - visible + 1)
            rows += [f'{">" if i == selected else " "} {label}' for i, (label, _) in enumerate(actions) if first <= i < first + visible]
            rows += ['Working...' if future else 'Result / live server log:']
            rows += (output[-6:] if output else logs(runtime, 6).splitlines())
            for y, row in enumerate(rows[:max(0, height-2)]):
                try:
                    window.addstr(y, 0, tui.clip(row, max(0, width-1)), curses.A_BOLD if y == 0 else 0)
                except curses.error:
                    pass
            footer = ':' + command[-max(1, width-2):] if command is not None else 'Up/Down select | Enter run | : command | l logs | q quit'
            if pending_setup:
                footer = f'{pending_setup} [y/N]'
            if height > 1:
                try:
                    window.addstr(height-1, 0, tui.clip(footer, max(0, width-1)), curses.A_REVERSE)
                except curses.error:
                    pass
            window.refresh()
            key = window.getch()
            if pending_setup:
                if key in (ord('y'), ord('Y')):
                    future = executor.submit(install_and_retry, pending_setup, submitted, runtime, releases)
                    pending_setup = None
                elif key in (ord('n'), ord('N'), 27, 10, 13):
                    pending_setup = None
                    output = ['Installation cancelled.']
                continue
            if command is not None:
                if key == 27:
                    command = None
                elif key in (10, 13) and not future:
                    submitted = command
                    future = executor.submit(action, submitted, runtime, releases)
                    command = None
                elif key in (curses.KEY_BACKSPACE, 127, 8):
                    command = command[:-1]
                elif 32 <= key < 127:
                    command += chr(key)
                continue
            if key in (ord('q'), 27) and not future:
                return 0
            if key in (curses.KEY_DOWN, ord('j')):
                selected = (selected+1) % len(actions)
            elif key in (curses.KEY_UP, ord('k')):
                selected = (selected-1) % len(actions)
            elif key in (10, 13) and not future:
                selected_command = actions[selected][1]
                if selected_command == 'model pick':
                    actions, selected = model_choices(), 0
                    output = ['Select an installed model or use the configured provider.']
                elif selected_command == 'back':
                    actions, selected = ACTIONS, 0
                elif selected_command.startswith('model select'):
                    submitted = selected_command
                    future = executor.submit(action, submitted, runtime, releases)
                    actions, selected = ACTIONS, 0
                elif selected_command == 'model download':
                    command = selected_command + ' '
                else:
                    submitted = selected_command
                    future = executor.submit(action, submitted, runtime, releases)
            elif key == ord(':'):
                command = ''
            elif key == ord('l'):
                output = []
