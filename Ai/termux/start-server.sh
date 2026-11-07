#!/usr/bin/env bash
set -Eeuo pipefail

MODEL_DIR="${MODEL_DIR:-$HOME/models}"
CONFIG_DIR="$HOME/.config/superbot"
API_KEY_FILE="$CONFIG_DIR/model-api-key"
ACTIVE_MODEL_FILE="$CONFIG_DIR/active-model"
MODEL_PORT="${LLAMA_PORT:-8080}"
LLAMA_HOST="${LLAMA_HOST:-127.0.0.1}"
SUPERBOT_DIR="${SUPERBOT_DIR:-$HOME/super_bot}"
LAUNCHER_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
MAC_SERVER_URL="${MAC_SERVER_URL:-}"
CONTEXT_SIZE="${LLAMA_CTX_SIZE:-2048}"
THREADS="${LLAMA_THREADS:-4}"
SERVER_MODEL_ALIAS="superbot-phone"
GGUF_MAGIC="47475546"

QWEN_4B_REPO="Qwen/Qwen3-4B-GGUF"
QWEN_4B_FILE="Qwen3-4B-Q4_K_M.gguf"
QWEN_4B_BYTES=2497280256
QWEN_8B_REPO="Qwen/Qwen3-8B-GGUF"
QWEN_8B_FILE="Qwen3-8B-Q4_K_M.gguf"
QWEN_8B_BYTES=5027783488

GEMMA_7B_REPO="bartowski/gemma-1.1-7b-it-GGUF"
GEMMA_7B_FILE="gemma-1.1-7b-it-Q4_K_M.gguf"
GEMMA_7B_BYTES=5329759200
GEMMA_9B_REPO="bartowski/gemma-2-9b-it-GGUF"
GEMMA_9B_FILE="gemma-2-9b-it-Q4_K_M.gguf"
GEMMA_9B_BYTES=5761057728

SUPERBOT_CHATTER_REPO="Displaynamethecat/super-bot_chatter-0.1"
SUPERBOT_CHATTER_FILE="super-bot-gemma-2b.gguf"
SUPERBOT_CHATTER_BYTES=5235213792
LOCAL_MODEL_FEATURES_AVAILABLE=1
PYTHON_BIN=""

fail() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

is_termux() {
    [[ "${PREFIX:-}" == "/data/data/com.termux/files/usr" ]]
}

prepare_runtime() {
    if ! is_termux; then
        local answer
        printf '%s\n' \
            'Warning: this script is designed for Termux and may be unstable on this system.' >&2
        read -r -p "Download the files needed for local-model features? [y/N] " answer ||
            answer=""
        if [[ "$answer" != "y" && "$answer" != "Y" ]]; then
            LOCAL_MODEL_FEATURES_AVAILABLE=0
        fi
        return
    fi

    local -a packages=()
    command -v llama-server >/dev/null 2>&1 || packages+=(llama-cpp)
    command -v curl >/dev/null 2>&1 || packages+=(curl)
    if ! stat -c '%s' /dev/null >/dev/null 2>&1 || ! command -v od >/dev/null 2>&1; then
        packages+=(coreutils)
    fi
    if ((${#packages[@]})); then
        if is_termux; then
            pkg install -y "${packages[@]}"
        else
            fail "Missing required commands: ${packages[*]}. Install them, then run this script again."
        fi
    fi

    command -v llama-server >/dev/null 2>&1 || fail "llama-server is unavailable."
    command -v curl >/dev/null 2>&1 || fail "curl is unavailable."
    stat -c '%s' /dev/null >/dev/null 2>&1 || fail "GNU coreutils stat is unavailable."
    command -v od >/dev/null 2>&1 || fail "od is unavailable."
}

require_local_model_features() {
    if ((LOCAL_MODEL_FEATURES_AVAILABLE)); then
        return 0
    fi
    printf '%s\n' \
        'Local-model files were not downloaded, so this option is unavailable.' \
        'Choose option 6 and select Gemini or OpenAI to start the secure cloud server.' >&2
    return 1
}

find_python() {
    if command -v python >/dev/null 2>&1; then
        PYTHON_BIN="$(command -v python)"
    elif command -v python3 >/dev/null 2>&1; then
        PYTHON_BIN="$(command -v python3)"
    else
        return 1
    fi
}

validate_runtime_settings() {
    [[ "$MODEL_PORT" =~ ^[0-9]+$ ]] && ((10#$MODEL_PORT >= 1 && 10#$MODEL_PORT <= 65535)) || fail \
        "LLAMA_PORT must be between 1 and 65535."
    [[ "$CONTEXT_SIZE" =~ ^[0-9]+$ ]] && ((10#$CONTEXT_SIZE >= 512 && 10#$CONTEXT_SIZE <= 32768)) || fail \
        "LLAMA_CTX_SIZE must be between 512 and 32768."
    [[ "$THREADS" =~ ^[0-9]+$ ]] && ((10#$THREADS >= 1 && 10#$THREADS <= 64)) || fail \
        "LLAMA_THREADS must be between 1 and 64."

    MODEL_PORT=$((10#$MODEL_PORT))
    CONTEXT_SIZE=$((10#$CONTEXT_SIZE))
    THREADS=$((10#$THREADS))

    if [[ -n "${LLAMA_SSL_CERT:-}" || -n "${LLAMA_SSL_KEY:-}" ]]; then
        [[ -f "${LLAMA_SSL_CERT:-}" && -f "${LLAMA_SSL_KEY:-}" ]] || fail \
            "Set both LLAMA_SSL_CERT and LLAMA_SSL_KEY to existing PEM files."
    fi
}

initialize_config() {
    mkdir -p "$MODEL_DIR" "$CONFIG_DIR"
    chmod 700 "$CONFIG_DIR" || fail "Failed to set permissions on $CONFIG_DIR."

    if [[ ! -s "$API_KEY_FILE" ]]; then
        umask 077
        od -vAn -N32 -tx1 /dev/urandom | tr -d ' \n' > "$API_KEY_FILE"
    fi
    chmod 600 "$API_KEY_FILE" || fail "Failed to set permissions on $API_KEY_FILE."

    active_model
    if [[ -z "$ACTIVE_MODEL" ]]; then
        installed_models
        if ((${#MODEL_FILES[@]} == 1)); then
            printf '%s\n' "${MODEL_FILES[0]##*/}" > "$ACTIVE_MODEL_FILE"
            chmod 600 "$ACTIVE_MODEL_FILE" || fail "Failed to set permissions on $ACTIVE_MODEL_FILE."
        fi
    fi
}

valid_model_filename() {
    [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._+-]*\.gguf$ ]]
}

valid_hf_repo() {
    [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$ ]]
}

valid_revision() {
    [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]
}

installed_models() {
    shopt -s nullglob
    MODEL_FILES=("$MODEL_DIR"/*.gguf)
    shopt -u nullglob
}

active_model() {
    ACTIVE_MODEL=""
    if [[ -f "$ACTIVE_MODEL_FILE" ]]; then
        IFS= read -r ACTIVE_MODEL < "$ACTIVE_MODEL_FILE" || true
    fi
    valid_model_filename "$ACTIVE_MODEL" || ACTIVE_MODEL=""
}

human_size() {
    local bytes="$1"
    awk -v bytes="$bytes" 'BEGIN {
        split("B KiB MiB GiB TiB", units, " ")
        unit = 1
        while (bytes >= 1024 && unit < 5) {
            bytes /= 1024
            unit++
        }
        printf "%.1f %s", bytes, units[unit]
    }'
}

available_bytes() {
    df -Pk "$MODEL_DIR" | awk 'END {printf "%.0f\n", $4 * 1024}'
}

list_models() {
    installed_models
    active_model
    if ((${#MODEL_FILES[@]} == 0)); then
        printf 'No GGUF models found in %s.\n' "$MODEL_DIR"
        return
    fi

    printf 'Models in %s:\n' "$MODEL_DIR"
    local model_path model_name size marker
    for model_path in "${MODEL_FILES[@]}"; do
        model_name="${model_path##*/}"
        size="$(stat -c '%s' "$model_path")"
        marker=" "
        [[ "$model_name" == "$ACTIVE_MODEL" ]] && marker="*"
        printf ' %s %-42s %s\n' "$marker" "$model_name" "$(human_size "$size")"
    done
    if [[ -n "$ACTIVE_MODEL" && ! -f "$MODEL_DIR/$ACTIVE_MODEL" ]]; then
        printf 'Warning: selected model is missing: %s\n' "$ACTIVE_MODEL"
    fi
    printf '* is the model selected for the next server launch.\n'
}

select_model() {
    installed_models
    ((${#MODEL_FILES[@]})) || {
        printf 'Download a GGUF model before selecting one.\n'
        return 1
    }

    active_model
    printf 'Select a model:\n'
    local index model_path model_name choice
    for index in "${!MODEL_FILES[@]}"; do
        model_name="${MODEL_FILES[$index]##*/}"
        if [[ "$model_name" == "$ACTIVE_MODEL" ]]; then
            printf ' %d) %s (currently selected)\n' "$((index + 1))" "$model_name"
        else
            printf ' %d) %s\n' "$((index + 1))" "$model_name"
        fi
    done
    read -r -p "Model number (blank to cancel): " choice || return 0
    [[ -n "$choice" ]] || return 0
    [[ "$choice" =~ ^[0-9]+$ ]] || {
        printf 'Enter a model number.\n'
        return 1
    }
    index=$((10#$choice - 1))
    ((index >= 0 && index < ${#MODEL_FILES[@]})) || {
        printf 'That model number is out of range.\n'
        return 1
    }
    model_path="${MODEL_FILES[$index]}"
    model_name="${model_path##*/}"
    printf '%s\n' "$model_name" > "$ACTIVE_MODEL_FILE"
    chmod 600 "$ACTIVE_MODEL_FILE" || fail "Failed to set permissions on $ACTIVE_MODEL_FILE."
    printf 'Selected %s. Super Bot will continue using API model name %s.\n' \
        "$model_name" "$SERVER_MODEL_ALIAS"
}

download_model() {
    local repo="$1"
    local filename="$2"
    local revision="${3:-main}"
    local expected_bytes="${4:-0}"
    local destination="$MODEL_DIR/$filename"
    local partial="$destination.part"
    local free_bytes url magic partial_bytes=0 required_bytes downloaded_bytes
    local -a curl_args=(
        --fail
        --location
        --retry 3
        --continue-at -
        --output "$partial"
    )

    valid_hf_repo "$repo" || fail "Use a Hugging Face repository in owner/name form."
    valid_model_filename "$filename" || fail "Filename must be a simple .gguf filename."
    valid_revision "$revision" || fail "Revision may contain only letters, numbers, dot, underscore, or hyphen."
    [[ "$expected_bytes" =~ ^[0-9]+$ ]] || fail "Internal model size must be numeric."
    [[ ! -e "$destination" ]] || {
        printf 'Already downloaded: %s\n' "$filename"
        return 0
    }
    [[ ! -L "$partial" ]] || fail "Refusing to write through a symlink at $partial."

    free_bytes="$(available_bytes)"
    printf 'Free space in %s: %s\n' "$MODEL_DIR" "$(human_size "$free_bytes")"
    if ((expected_bytes > 0)); then
        [[ -f "$partial" ]] && partial_bytes="$(stat -c '%s' "$partial")"
        ((partial_bytes <= expected_bytes)) || {
            printf 'Partial download is larger than the expected model; remove %s and retry.\n' \
                "$partial"
            return 1
        }
        required_bytes=$((expected_bytes - partial_bytes + 100000000))
        printf 'Expected model size: %s; this download needs about %s more free space.\n' \
            "$(human_size "$expected_bytes")" "$(human_size "$required_bytes")"
        ((free_bytes >= required_bytes)) || {
            printf 'Not enough free space for this model; free more space or choose another.\n'
            return 1
        }
    else
        printf 'Model size is unknown. Check that you have enough free space before continuing.\n'
        read -r -p "Continue with this download? [y/N] " answer || return 0
        [[ "$answer" == "y" || "$answer" == "Y" ]] || return 0
    fi

    url="https://huggingface.co/$repo/resolve/$revision/$filename"
    if [[ -n "${HF_TOKEN:-}" ]]; then
        curl_args+=(--header "Authorization: Bearer $HF_TOKEN")
    fi
    printf 'Downloading %s from %s (re-running resumes a partial download)...\n' \
        "$filename" "$repo"
    if ! curl "${curl_args[@]}" "$url"; then
        printf 'Download failed. Partial data is kept at %s for resuming.\n' "$partial" >&2
        return 1
    fi

    downloaded_bytes="$(stat -c '%s' "$partial")"
    if ((expected_bytes > 0 && downloaded_bytes != expected_bytes)); then
        printf 'Download size mismatch: expected %s bytes, got %s. Partial data was kept for resuming.\n' \
            "$expected_bytes" "$downloaded_bytes" >&2
        return 1
    fi
    magic="$(od -An -tx1 -N4 "$partial" | tr -d ' \n')"
    if [[ "$magic" != "$GGUF_MAGIC" ]]; then
        printf 'Downloaded file is not a GGUF model; removing the invalid partial file.\n' >&2
        rm -f "$partial"
        return 1
    fi
    mv "$partial" "$destination"
    active_model
    if [[ -z "$ACTIVE_MODEL" ]]; then
        printf '%s\n' "$filename" > "$ACTIVE_MODEL_FILE"
        chmod 600 "$ACTIVE_MODEL_FILE" || fail "Failed to set permissions on $ACTIVE_MODEL_FILE."
    fi
    printf 'Installed %s.\n' "$filename"
}

download_superbot_chatter() {
    printf '\nDownloading Super Bot Chatter.\n'
    download_model \
        "$SUPERBOT_CHATTER_REPO" \
        "$SUPERBOT_CHATTER_FILE" \
        main \
        "$SUPERBOT_CHATTER_BYTES"
}

download_menu() {
    printf '\nDownload a model:\n'
    printf '  1) Qwen3 4B Q4_K_M (about 2.5 GB)\n'
    printf '  2) Qwen3 8B Q4_K_M (about 5.0 GB; likely slower and tighter on phone RAM)\n'
    printf '  3) Gemma 1.1 7B instruct Q4_K_M (about 5.3 GB; needs roughly 7+ GB free RAM)\n'
    printf '  4) Gemma 2 9B instruct Q4_K_M (about 5.8 GB; best quality, tightest on RAM)\n'
    printf '  5) Other Hugging Face GGUF (enter repository and filename)\n'
    printf '  6) Super Bot Chatter (about 5.2 GB)\n'
    printf '  0) Cancel\n'
    read -r -p "Choice: " choice || return 0
    case "$choice" in
        1)
            download_model "$QWEN_4B_REPO" "$QWEN_4B_FILE" main "$QWEN_4B_BYTES"
            ;;
        2)
            download_model "$QWEN_8B_REPO" "$QWEN_8B_FILE" main "$QWEN_8B_BYTES"
            ;;
        3)
            download_model "$GEMMA_7B_REPO" "$GEMMA_7B_FILE" main "$GEMMA_7B_BYTES"
            ;;
        4)
            download_model "$GEMMA_9B_REPO" "$GEMMA_9B_FILE" main "$GEMMA_9B_BYTES"
            ;;
        5)
            read -r -p "Hugging Face repo (owner/name): " repo || return 1
            read -r -p "Exact .gguf filename: " filename || return 1
            read -r -p "Revision/branch [main]: " revision || return 1
            revision="${revision:-main}"
            if [[ -n "${HF_TOKEN:-}" ]]; then
                printf 'Using HF_TOKEN from the environment for gated/private access.\n'
            fi
            download_model "$repo" "$filename" "$revision"
            ;;
        6)
            download_superbot_chatter
            ;;
        0|"")
            ;;
        *)
            printf 'Unknown choice.\n'
            return 1
            ;;
    esac
}

remove_model() {
    local filename="$1"
    local target="$MODEL_DIR/$filename"
    local answer
    valid_model_filename "$filename" || fail "Specify a simple .gguf filename."
    [[ -f "$target" && ! -L "$target" ]] || {
        printf 'Model not found: %s\n' "$filename"
        return 1
    }
    active_model
    if [[ "$filename" == "$ACTIVE_MODEL" ]]; then
        printf 'Select a different active model before deleting this one.\n'
        return 1
    fi
    printf 'This permanently deletes %s (%s).\n' "$filename" \
        "$(human_size "$(stat -c '%s' "$target")")"
    read -r -p "Continue? [y/N] " answer || return 0
    [[ "$answer" == "y" || "$answer" == "Y" ]] || return 0
    rm -- "$target"
    printf 'Deleted %s.\n' "$filename"
}

start_server() {
    active_model
    [[ -n "$ACTIVE_MODEL" && -f "$MODEL_DIR/$ACTIVE_MODEL" ]] || {
        printf 'Select an installed model before starting the server.\n'
        return 1
    }

    local scheme="http"
    local -a tls_args=()
    if [[ -n "${LLAMA_SSL_CERT:-}" ]]; then
        scheme="https"
        tls_args=(--ssl-cert-file "$LLAMA_SSL_CERT" --ssl-key-file "$LLAMA_SSL_KEY")
    fi

    if [[ "${1:-}" != supervised ]]; then
        cat <<EOF

Starting the local model API.
  Model:     $ACTIVE_MODEL
  API name:  $SERVER_MODEL_ALIAS
  Base URL:  $scheme://$LLAMA_HOST:$MODEL_PORT/v1
  API key:   $(cat "$API_KEY_FILE")
  Key file:  $API_KEY_FILE

Listening on $LLAMA_HOST only. Use menu option 6 to expose the model through the encrypted Super Bot server instead.
Press Ctrl+C to stop the server and return to this menu.

EOF
        if [[ "$scheme" == "http" ]]; then
            printf 'Note: this link is unencrypted. See README.md to enable HTTPS (LLAMA_SSL_CERT, LLAMA_SSL_KEY).\n\n'
        fi
    fi
    local -a model_args=(
        --model "$MODEL_DIR/$ACTIVE_MODEL"
        --alias "$SERVER_MODEL_ALIAS"
        --host "$LLAMA_HOST"
        --port "$MODEL_PORT"
        --api-key-file "$API_KEY_FILE"
        --ctx-size "$CONTEXT_SIZE"
        --threads "$THREADS"
        --parallel 1
        "${tls_args[@]}"
    )
    if [[ "${1:-}" == supervised ]]; then
        exec llama-server "${model_args[@]}"
    fi
    if ! llama-server "${model_args[@]}"; then
        printf 'Model server stopped or failed to start. Check the message above.\n' >&2
        return 1
    fi
}

start_secure_stack() (
    local provider_choice=1
    local gemini_api_key="${GEMINI_API_KEY:-}"
    local openai_api_key="${OPENAI_API_KEY:-}"
    local openai_model_name="${OPENAI_MODEL_NAME:-}"
    printf '\nChoose the AI model provider for this server:\n'
    printf '  1) Use the local model on this phone (default)\n'
    printf '  2) Use Google Gemini cloud (chat content is sent to Google)\n'
    printf '  3) Use OpenAI cloud (chat content is sent to OpenAI)\n'
    if ! read -r -p "Choice [1]: " provider_choice || [[ -z "$provider_choice" ]]; then
        provider_choice=1
    fi
    case "$provider_choice" in
        1) provider_choice=phone ;;
        2)
            provider_choice=gemini
            if [[ -z "$gemini_api_key" ]]; then
                read -r -s -p "Gemini API key (input hidden): " gemini_api_key ||
                    fail "Could not read the Gemini API key."
                printf '\n'
            fi
            [[ -n "$gemini_api_key" ]] || fail "A Gemini API key is required."
            ;;
        3)
            provider_choice=openai
            if [[ -z "$openai_api_key" ]]; then
                read -r -s -p "OpenAI API key (input hidden): " openai_api_key ||
                    fail "Could not read the OpenAI API key."
                printf '\n'
            fi
            [[ -n "$openai_api_key" ]] || fail "An OpenAI API key is required."
            if [[ -z "$openai_model_name" ]]; then
                read -r -p "OpenAI model ID: " openai_model_name || fail "Could not read the model ID."
            fi
            [[ -n "$openai_model_name" ]] || fail "An OpenAI model ID is required."
            export OPENAI_API_KEY="$openai_api_key"
            export OPENAI_MODEL_NAME="$openai_model_name"
            ;;
        *) fail "Choose 1 for the phone model, 2 for Gemini, or 3 for OpenAI." ;;
    esac

    # Prefer the shared manager whenever the package is installed. The old signed
    # launcher remains a compatibility path for installations not yet migrated.
    if find_python && "$PYTHON_BIN" -c 'import Ai.manager_cli' 2>/dev/null; then
        if [[ "$provider_choice" == phone ]]; then
            export PHONE_MODEL_PROVIDER=phone
            export PHONE_MODEL_BASE_URL="http://127.0.0.1:$MODEL_PORT/v1"
            export TEST_PHONE_API_KEY="$(cat "$API_KEY_FILE")"
            printf 'Start the local model with option 5 when inference is needed.\n'
        else
            export PHONE_MODEL_PROVIDER="$provider_choice"
            export GEMINI_API_KEY="$gemini_api_key"
        fi
        if [[ "${1:-start}" == update ]]; then
            if [[ "${SUPER_BOT_OFFLINE:-0}" == 1 ]]; then
                "$PYTHON_BIN" -m Ai.manager_cli update apply --offline || return
            else
                "$PYTHON_BIN" -m Ai.manager_cli update apply || return
            fi
        fi
        "$PYTHON_BIN" -m Ai.manager_cli start --foreground
        return
    fi

    local updater="$LAUNCHER_DIR/update-server.py"
    local requirements="$LAUNCHER_DIR/requirements.txt"
    local -a update_args=()
    [[ -f "$requirements" ]] || requirements="$LAUNCHER_DIR/../requirements.txt"
    [[ -f "$updater" ]] || fail "Copy the trusted update-server.py alongside this launcher."
    local key="${SUPER_BOT_RELEASE_KEY:-$CONFIG_DIR/release-public.pem}"
    if [[ -z "${SUPER_BOT_RELEASE_KEY:-}" && ! -f "$key" ]]; then
        key="$LAUNCHER_DIR/release-public.pem"
    fi
    if [[ "${1:-start}" != update || "${SUPER_BOT_OFFLINE:-0}" == 1 ]]; then
        printf 'Starting the installed server in offline mode.\n'
    elif [[ -z "$MAC_SERVER_URL" ]]; then
        fail "Use super-bot-manager update apply for official releases, or explicitly configure MAC_SERVER_URL for the legacy signed updater."
    elif [[ -f "$key" ]]; then
        update_args=(--url "$MAC_SERVER_URL" --key "$key")
    else
        fail "No trusted release key at $key. Download the public key over a trusted connection before updating."
    fi
    if ! find_python; then
        if is_termux; then
            pkg install -y python
            find_python || fail "Python is required to start the secure server."
        else
            fail "Python is required to start the secure server."
        fi
    fi
    if ((${#update_args[@]})) && ! command -v openssl >/dev/null 2>&1; then
        if is_termux; then
            pkg install -y openssl-tool || fail "Could not install OpenSSL."
        else
            fail "OpenSSL is required to verify releases."
        fi
    fi
    if ! "$PYTHON_BIN" -c 'import argon2, cryptography, dotenv, fastapi, httpx, pydantic_core, uvicorn, websockets' 2>/dev/null; then
        [[ -f "$requirements" ]] || fail "Copy the trusted requirements.txt alongside this launcher."
        if is_termux; then
            fail "Install the Super Bot wheel with install-wheel.sh first (see the Termux README)."
        fi
        "$PYTHON_BIN" -m pip install --only-binary=:all: -r "$requirements" ||
            fail "Prebuilt server dependencies are unavailable for this Python."
    fi
    "$PYTHON_BIN" -c 'import argon2, cryptography, dotenv, fastapi, httpx, pydantic_core, uvicorn, websockets' ||
        fail "Python dependencies are still unavailable after installation."
    if [[ "$provider_choice" == phone ]]; then
        local model_pid=""
        local deadline
        local -a model_probe=(curl --fail --silent --max-time 2
            --header "Authorization: Bearer $(cat "$API_KEY_FILE")"
            "http://127.0.0.1:$MODEL_PORT/v1/models")
        if "${model_probe[@]}" >/dev/null; then
            printf 'Reusing the existing local model API on 127.0.0.1:%s.\n' "$MODEL_PORT"
        else
            LLAMA_HOST=127.0.0.1 start_server supervised &
            model_pid=$!
            trap 'kill "$model_pid" 2>/dev/null || true; wait "$model_pid" 2>/dev/null || true' EXIT
            deadline=$((SECONDS + 120))
            until "${model_probe[@]}" >/dev/null; do
                if ! kill -0 "$model_pid" 2>/dev/null || ((SECONDS >= deadline)); then
                    printf 'The local model API did not become ready. Check the model server log.\n' >&2
                    return 1
                fi
                sleep 1
            done
            printf 'Local model API is ready on 127.0.0.1:%s.\n' "$MODEL_PORT"
        fi
        export PHONE_MODEL_PROVIDER=phone
        export PHONE_MODEL_BASE_URL="http://127.0.0.1:$MODEL_PORT/v1"
        export TEST_PHONE_API_KEY="$(cat "$API_KEY_FILE")"
    else
        export PHONE_MODEL_PROVIDER="$provider_choice"
        export GEMINI_API_KEY="$gemini_api_key"
    fi
    "$PYTHON_BIN" "$updater" \
        --install "$SUPERBOT_DIR" "${update_args[@]}"
)

show_menu() {
    printf '\nSUPER BOT PHONE MODEL MANAGER\n'
    printf '  1) List installed models\n'
    printf '  2) Download a model\n'
    printf '  3) Select active model\n'
    printf '  4) Delete an inactive model\n'
    printf '  5) Start the model API only (local)\n'
    printf '  6) Start installed secure server (phone, Gemini, or OpenAI)\n'
    printf '  7) Download a signed update and start the secure server\n'
    printf '  0) Exit\n'
    read -r -p "Choice: " choice || return 1
    case "$choice" in
        1) require_local_model_features && list_models ;;
        2) require_local_model_features && download_menu ;;
        3) require_local_model_features && select_model ;;
        4)
            if require_local_model_features; then
                list_models
                read -r -p "Exact model filename to delete (blank to cancel): " filename || filename=""
                [[ -n "$filename" ]] && remove_model "$filename"
            fi
            ;;
        5) require_local_model_features && start_server ;;
        6) start_secure_stack || printf 'Secure server exited or the update failed.\n' >&2 ;;
        7) start_secure_stack update || printf 'Update or server startup failed.\n' >&2 ;;
        0) return 1 ;;
        *) printf 'Unknown choice.\n' ;;
    esac
    return 0
}

main() {
    case "${1:-}" in
        --help|-h)
            printf 'Usage: bash start-server.sh [--list|--start|--download REPO FILE [REVISION]|--select FILE|--remove FILE]\n'
            printf 'Use super-bot-manager for signed official releases; MAC_SERVER_URL is only for explicit legacy LAN updates.\n'
            printf 'Run without arguments for the interactive model manager.\n'
            return 0
    esac

    prepare_runtime
    initialize_config
    validate_runtime_settings

    case "${1:-}" in
        --list)
            require_local_model_features && list_models
            return 0
            ;;
        --start)
            require_local_model_features && start_server
            return
            ;;
        --download)
            require_local_model_features || return 0
            (($# >= 3 && $# <= 4)) || fail "Use --download OWNER/REPO FILE.gguf [REVISION]."
            download_model "$2" "$3" "${4:-main}"
            return
            ;;
        --select)
            require_local_model_features || return 0
            (($# == 2)) || fail "Use --select FILE.gguf."
            valid_model_filename "$2" || fail "Specify a simple .gguf filename."
            [[ -f "$MODEL_DIR/$2" ]] || fail "Model is not installed: $2"
            printf '%s\n' "$2" > "$ACTIVE_MODEL_FILE"
            chmod 600 "$ACTIVE_MODEL_FILE"
            printf 'Selected %s. API model name remains %s.\n' "$2" "$SERVER_MODEL_ALIAS"
            return 0
            ;;
        --remove)
            require_local_model_features || return 0
            (($# == 2)) || fail "Use --remove FILE.gguf."
            remove_model "$2"
            return
            ;;
        "")
            ;;
        *)
            fail "Unknown option: $1 (use --help)."
            ;;
    esac

    while show_menu; do
        :
    done
}

main "$@"
