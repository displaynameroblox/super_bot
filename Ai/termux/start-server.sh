#!/data/data/com.termux/files/usr/bin/bash
set -Eeuo pipefail

MODEL_DIR="${MODEL_DIR:-$HOME/models}"
CONFIG_DIR="$HOME/.config/superbot"
API_KEY_FILE="$CONFIG_DIR/model-api-key"
ACTIVE_MODEL_FILE="$CONFIG_DIR/active-model"
PORT="${LLAMA_PORT:-8080}"
LLAMA_HOST="${LLAMA_HOST:-127.0.0.1}"
SUPERBOT_DIR="${SUPERBOT_DIR:-$HOME/super_bot}"
MAC_SERVER_URL="${MAC_SERVER_URL:-http://192.168.8.179:8765}"
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

fail() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

require_termux() {
    command -v pkg >/dev/null 2>&1 || fail "Run this script inside Termux."

    local -a packages=()
    command -v llama-server >/dev/null 2>&1 || packages+=(llama-cpp)
    command -v curl >/dev/null 2>&1 || packages+=(curl)
    command -v wget >/dev/null 2>&1 || packages+=(wget)
    if ! stat -c '%s' /dev/null >/dev/null 2>&1 || ! command -v od >/dev/null 2>&1; then
        packages+=(coreutils)
    fi
    if ((${#packages[@]})); then
        pkg install -y "${packages[@]}"
    fi

    command -v llama-server >/dev/null 2>&1 || fail "llama-server is unavailable after installing llama-cpp."
    command -v curl >/dev/null 2>&1 || fail "curl is unavailable after package installation."
    command -v wget >/dev/null 2>&1 || fail "wget is unavailable after package installation."
    stat -c '%s' /dev/null >/dev/null 2>&1 || fail "GNU coreutils stat is unavailable."
    command -v od >/dev/null 2>&1 || fail "od is unavailable after package installation."
}

validate_runtime_settings() {
    [[ "$PORT" =~ ^[0-9]+$ ]] && ((10#$PORT >= 1 && 10#$PORT <= 65535)) || fail \
        "LLAMA_PORT must be between 1 and 65535."
    [[ "$CONTEXT_SIZE" =~ ^[0-9]+$ ]] && ((10#$CONTEXT_SIZE >= 512 && 10#$CONTEXT_SIZE <= 32768)) || fail \
        "LLAMA_CTX_SIZE must be between 512 and 32768."
    [[ "$THREADS" =~ ^[0-9]+$ ]] && ((10#$THREADS >= 1 && 10#$THREADS <= 64)) || fail \
        "LLAMA_THREADS must be between 1 and 64."

    PORT=$((10#$PORT))
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

download_trained_model() {
    printf '\nExperimental locally trained model — still testing.\n'
    download_model \
        "Displaynamethecat/gemma-2-2b-it-friendly-gguf" \
        "gemma-2-2b-it.Q4_K_M.gguf" \
        main \
        1708582560
}

download_menu() {
    printf '\nDownload a model:\n'
    printf '  1) Qwen3 4B Q4_K_M (about 2.5 GB)\n'
    printf '  2) Qwen3 8B Q4_K_M (about 5.0 GB; likely slower and tighter on phone RAM)\n'
    printf '  3) Gemma 1.1 7B instruct Q4_K_M (about 5.3 GB; needs roughly 7+ GB free RAM)\n'
    printf '  4) Gemma 2 9B instruct Q4_K_M (about 5.8 GB; best quality, tightest on RAM)\n'
    printf '  5) Other Hugging Face GGUF (enter repository and filename)\n'
    printf '  6) Download experimental locally trained model from Hugging Face (still testing)\n'
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
            download_trained_model
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

    cat <<EOF

Starting the local model API.
  Model:     $ACTIVE_MODEL
  API name:  $SERVER_MODEL_ALIAS
  Base URL:  $scheme://$LLAMA_HOST:$PORT/v1
  API key:   $(cat "$API_KEY_FILE")
  Key file:  $API_KEY_FILE

Listening on $LLAMA_HOST only. Use menu option 6 to expose the model through the encrypted Super Bot server instead.
Press Ctrl+C to stop the server and return to this menu.

EOF
    if [[ "$scheme" == "http" ]]; then
        printf 'Note: this link is unencrypted. See README.md to enable HTTPS (LLAMA_SSL_CERT, LLAMA_SSL_KEY).\n\n'
    fi
    if ! llama-server \
        --model "$MODEL_DIR/$ACTIVE_MODEL" \
        --alias "$SERVER_MODEL_ALIAS" \
        --host "$LLAMA_HOST" \
        --port "$PORT" \
        --api-key-file "$API_KEY_FILE" \
        --ctx-size "$CONTEXT_SIZE" \
        --threads "$THREADS" \
        --parallel 1 \
        ${tls_args[@]+"${tls_args[@]}"}; then
        printf 'Model server stopped or failed to start. Check the message above.\n' >&2
        return 1
    fi
}

start_secure_stack() {
    local provider_choice=1
    local gemini_api_key="${GEMINI_API_KEY:-}"
    printf '\nChoose the AI model provider for this server:\n'
    printf '  1) Use the local model on this phone (default)\n'
    printf '  2) Use Google Gemini cloud (chat content is sent to Google)\n'
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
        *) fail "Choose 1 for the phone model or 2 for Gemini." ;;
    esac

    mkdir -p "$SUPERBOT_DIR/Ai"
    printf 'Downloading Super Bot files from %s into %s/Ai...\n' "$MAC_SERVER_URL" "$SUPERBOT_DIR"
    if ! wget \
        --recursive \
        --no-parent \
        --no-host-directories \
        --timestamping \
        --reject 'index.html*' \
        --directory-prefix "$SUPERBOT_DIR/Ai" \
        "${MAC_SERVER_URL%/}/"; then
        fail "Could not download the Ai folder. Start Ai/termux/serve-from-mac.sh on the Mac and check MAC_SERVER_URL."
    fi
    [[ -f "$SUPERBOT_DIR/Ai/server.py" && -f "$SUPERBOT_DIR/Ai/requirements.txt" ]] ||
        fail "The download did not include Ai/server.py and Ai/requirements.txt; check that the Mac serves the Ai folder."
    command -v python >/dev/null 2>&1 || pkg install -y python
    pkg install -y python-cryptography
    if ! python -c 'import fastapi, uvicorn, httpx, websockets, cryptography, pydantic_core' 2>/dev/null; then
        if python -m pip install \
            --only-binary=:all: \
            --no-deps \
            --index-url https://eutalix.github.io/android-pydantic-core/ \
            pydantic-core; then
            python -m pip install --only-binary=pydantic-core \
                -r "$SUPERBOT_DIR/Ai/requirements.txt" ||
                fail "Could not install the Super Bot Python requirements."
        else
            printf 'No compatible prebuilt pydantic-core wheel; installing Rust and the Termux maturin wheel to build it on this phone. This may take a while.\n'
            pkg install -y rust || fail "Could not install Rust required to build pydantic-core."
            python -m pip install \
                --only-binary=maturin \
                --extra-index-url https://termux-user-repository.github.io/pypi/ \
                maturin ||
                fail "Could not install the prebuilt Termux maturin build tool."
            python -m pip install \
                --no-build-isolation \
                --no-binary=pydantic-core \
                pydantic-core ||
                fail "Could not build pydantic-core with Rust. Check the build output and available memory."
            python -m pip install --only-binary=pydantic-core \
                -r "$SUPERBOT_DIR/Ai/requirements.txt" ||
                fail "Could not install the Super Bot Python requirements."
        fi
    fi
    python -c 'import fastapi, uvicorn, httpx, websockets, cryptography, pydantic_core' ||
        fail "Python dependencies are still unavailable after installation."
    if [[ "$provider_choice" == phone ]]; then
        LLAMA_HOST=127.0.0.1 start_server &
        local model_pid=$!
        trap "kill \"$model_pid\" 2>/dev/null || true" RETURN
        sleep 3
        printf 'Model is bound to 127.0.0.1 only; only the encrypted Super Bot port is reachable.\n'
        (
            cd "$SUPERBOT_DIR"
            PHONE_MODEL_PROVIDER=phone \
            PHONE_MODEL_BASE_URL="http://127.0.0.1:$PORT/v1" \
            TEST_PHONE_API_KEY="$(cat "$API_KEY_FILE")" \
            HOST=0.0.0.0 python -m Ai.server
        )
    else
        (
            cd "$SUPERBOT_DIR"
            PHONE_MODEL_PROVIDER=gemini \
            GEMINI_API_KEY="$gemini_api_key" \
            HOST=0.0.0.0 python -m Ai.server
        )
    fi
}

show_menu() {
    printf '\nSUPER BOT PHONE MODEL MANAGER\n'
    printf '  1) List installed models\n'
    printf '  2) Download a model\n'
    printf '  3) Select active model\n'
    printf '  4) Delete an inactive model\n'
    printf '  5) Start the model API only (local)\n'
    printf '  6) Download/update files and start the secure server (phone or Gemini)\n'
    printf '  0) Exit\n'
    read -r -p "Choice: " choice || return 1
    case "$choice" in
        1) list_models ;;
        2) download_menu ;;
        3) select_model ;;
        4)
            list_models
            read -r -p "Exact model filename to delete (blank to cancel): " filename || filename=""
            [[ -n "$filename" ]] && remove_model "$filename"
            ;;
        5) start_server ;;
        6) start_secure_stack ;;
        0) return 1 ;;
        *) printf 'Unknown choice.\n' ;;
    esac
    return 0
}

main() {
    case "${1:-}" in
        --help|-h)
            printf 'Usage: bash start-server.sh [--list|--start|--download REPO FILE [REVISION]|--select FILE|--remove FILE]\n'
            printf 'Set MAC_SERVER_URL to override the Mac download URL (default: %s).\n' "$MAC_SERVER_URL"
            printf 'Run without arguments for the interactive model manager.\n'
            return 0
    esac

    require_termux
    initialize_config
    validate_runtime_settings

    case "${1:-}" in
        --list)
            list_models
            return 0
            ;;
        --start)
            start_server
            return
            ;;
        --download)
            (($# >= 3 && $# <= 4)) || fail "Use --download OWNER/REPO FILE.gguf [REVISION]."
            download_model "$2" "$3" "${4:-main}"
            return
            ;;
        --select)
            (($# == 2)) || fail "Use --select FILE.gguf."
            valid_model_filename "$2" || fail "Specify a simple .gguf filename."
            [[ -f "$MODEL_DIR/$2" ]] || fail "Model is not installed: $2"
            printf '%s\n' "$2" > "$ACTIVE_MODEL_FILE"
            chmod 600 "$ACTIVE_MODEL_FILE"
            printf 'Selected %s. API model name remains %s.\n' "$2" "$SERVER_MODEL_ALIAS"
            return 0
            ;;
        --remove)
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
