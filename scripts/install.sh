#!/usr/bin/env bash
# Orbwise – installer for Arch Linux / Manjaro / EndeavourOS and Debian / Ubuntu / Linux Mint
#
#   ./scripts/install.sh                   # asks for language, GPU and model (with recommendations)
#   ./scripts/install.sh --lang en         # English assistant, voice and UI (default: de)
#   ./scripts/install.sh --model qwen3:8b  # choose the Ollama model yourself
#   ./scripts/install.sh --gpu vulkan      # force the Ollama backend: auto | cuda | rocm | vulkan | cpu (Arch)
#   ./scripts/install.sh --yes             # no questions, use the detected/recommended defaults
#   ./scripts/install.sh --no-autostart
# Change or add models later with:  orbwise model add
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/orbwise"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}/orbwise"
MODEL=""
GPU="auto"
LANG_CHOICE=""
AUTOSTART=1
ASK=1
REPO="${ORBWISE_REPO:-https://github.com/ressjo/orbwise-linux-agent.git}"
BRANCH="${ORBWISE_BRANCH:-main}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model) MODEL="$2"; shift 2 ;;
    --lang) LANG_CHOICE="$2"; shift 2 ;;
    --gpu) GPU="$2"; shift 2 ;;
    --vulkan) GPU="vulkan"; shift ;;   # kept for compatibility
    --cpu) GPU="cpu"; shift ;;
    --no-autostart) AUTOSTART=0; shift ;;
    -y|--yes) ASK=0; shift ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done
say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
[[ -t 0 ]] || ASK=0

# ---------------------------------------------------------------- Language
if [[ -z "$LANG_CHOICE" && $ASK == 1 ]]; then
  echo "Language / Sprache:"
  echo "  1) Deutsch"
  echo "  2) English"
  read -r -p "[1]: " answer
  [[ "$answer" == "2" ]] && LANG_CHOICE="en"
fi
[[ "$LANG_CHOICE" == "en" ]] || LANG_CHOICE="de"
export ORBWISE_LANG="$LANG_CHOICE"
t() { if [[ "$LANG_CHOICE" == "en" ]]; then printf '%s' "$2"; else printf '%s' "$1"; fi; }
ask_yes() {  # ask_yes "Frage" "Question" [default y|n]
  local def="${3:-y}" answer
  (( ASK )) || { [[ "$def" == "y" ]]; return; }
  read -r -p "$(t "$1" "$2") [$( [[ $def == y ]] && t 'J/n' 'Y/n' || t 'j/N' 'y/N')] " answer
  answer="${answer,,}"
  [[ -z "$answer" ]] && answer="$def"
  [[ "$answer" =~ ^(j|ja|y|yes)$ ]]
}

if command -v pacman >/dev/null; then DISTRO="arch"
elif command -v apt-get >/dev/null; then DISTRO="debian"
else echo "Unsupported distribution – Orbwise supports Arch-based (pacman) and Debian/Ubuntu-based (apt) systems."; exit 1
fi

# ---------------------------------------------------------------- Fixed location via git
# Started from a ZIP download? A git checkout makes updates a one-liner ("orbwise update").
if [[ ! -d "$ROOT/.git" && -t 0 ]]; then
  echo "This folder is a ZIP download. Updates are easier with a git checkout in ~/orbwise."
  read -r -p "Install to ~/orbwise instead (recommended)? [Y/n] " answer
  if [[ ! "$answer" =~ ^[nN] ]]; then
    if ! command -v git >/dev/null; then
      if [[ $DISTRO == arch ]]; then sudo pacman -S --needed --noconfirm git; else sudo apt-get install -y git; fi
    fi
    ORBWISE_REPO="$REPO" ORBWISE_BRANCH="$BRANCH" exec bash "$ROOT/scripts/bootstrap.sh" "$@"
  fi
fi

# ---------------------------------------------------------------- GPU
PCI="$(lspci 2>/dev/null | grep -iE 'vga|3d controller|display' || true)"
if command -v nvidia-smi >/dev/null && nvidia-smi -L >/dev/null 2>&1 || grep -qi nvidia <<<"$PCI"; then DETECTED="cuda"
elif ls /sys/class/drm/card*/device/mem_info_vram_total >/dev/null 2>&1 || grep -qiE 'amd|ati' <<<"$PCI"; then DETECTED="rocm"
else DETECTED="cpu"; fi
if [[ "$GPU" == "auto" ]]; then
  GPU="$DETECTED"
  if (( ASK )); then
    [[ -n "$PCI" ]] && printf '%s\n' "$(t 'Gefundene Grafikkarte(n):' 'Detected graphics card(s):')" "$PCI"
    echo "$(t 'Welche Grafikkarte soll das Sprachmodell nutzen?' 'Which graphics card should run the language model?')"
    echo "  1) NVIDIA (CUDA)"
    echo "  2) AMD (ROCm)"
    echo "  3) $(t 'Keine / nur CPU (langsam)' 'None / CPU only (slow)')"
    default=$([[ $DETECTED == cuda ]] && echo 1 || { [[ $DETECTED == rocm ]] && echo 2 || echo 3; })
    read -r -p "$(t 'Auswahl' 'Choice') [$default]: " answer
    case "${answer:-$default}" in 1) GPU="cuda" ;; 2) GPU="rocm" ;; *) GPU="cpu" ;; esac
    if [[ $GPU == rocm && $DISTRO == arch ]] && ! ask_yes \
        "ROCm verwenden? (Nein = Vulkan, für Karten ohne ROCm-Unterstützung)" \
        "Use ROCm? (No = Vulkan, for cards without ROCm support)" y; then
      GPU="vulkan"
    fi
  fi
fi
echo "GPU backend: $GPU"
if [[ $GPU == cuda ]] && ! command -v nvidia-smi >/dev/null; then
  echo "$(t '⚠ Kein NVIDIA-Treiber gefunden (nvidia-smi fehlt). Installieren mit:' '⚠ No NVIDIA driver found (nvidia-smi missing). Install it with:')"
  if [[ $DISTRO == arch ]]; then echo "    sudo pacman -S nvidia-open nvidia-utils   # $(t 'danach neu starten' 'then reboot')"
  else echo "    sudo ubuntu-drivers install   # $(t 'danach neu starten' 'then reboot')"; fi
  ask_yes "Trotzdem fortfahren (Modelle laufen bis dahin auf der CPU)?" \
          "Continue anyway (models run on the CPU until then)?" y || exit 1
fi
# RDNA2/RDNA3-Karten ohne offiziellen ROCm-Support laufen mit einem Override (z. B. RX 6600/6650/6700, RX 7600)
HSA=""
if [[ $GPU == rocm ]]; then
  if grep -qiE 'navi 2[234]' <<<"$PCI"; then HSA="10.3.0"; elif grep -qiE 'navi 3[23]' <<<"$PCI"; then HSA="11.0.0"; fi
fi

# ---------------------------------------------------------------- System packages
say "Installing system packages"
if [[ $DISTRO == arch ]]; then
  case "$GPU" in
    cuda) OLLAMA_PKG="ollama-cuda" ;;
    rocm) OLLAMA_PKG="ollama-rocm" ;;
    vulkan) OLLAMA_PKG="ollama-vulkan" ;;
    *) OLLAMA_PKG="ollama" ;;
  esac
  if ! pacman -Si "$OLLAMA_PKG" >/dev/null 2>&1; then
    echo "Package $OLLAMA_PKG not found in the repos – using 'ollama'."
    OLLAMA_PKG="ollama"
  fi
  sudo pacman -S --needed --noconfirm python uv git fd ripgrep plocate xdg-utils polkit pacman-contrib libnotify \
    gtk3 curl iproute2 iputils bubblewrap "$OLLAMA_PKG"
  sudo systemctl enable --now ollama.service
  sudo systemctl enable --now plocate-updatedb.timer || true
else
  sudo apt-get update
  sudo apt-get install -y python3 python3-venv git curl ca-certificates fd-find ripgrep plocate xdg-utils \
    libnotify-bin libgtk-3-bin iproute2 iputils-ping zstd bubblewrap
  # uv (Python package manager) – official installer, installs to ~/.local/bin
  if ! command -v uv >/dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
  fi
  # Ollama – official installer (sets up CUDA/ROCm support and the systemd service)
  if ! command -v ollama >/dev/null; then
    curl -fsSL https://ollama.com/install.sh | sh
  fi
  sudo systemctl enable --now ollama.service || true
fi
sudo updatedb || true

if [[ -n "$HSA" ]] && ask_yes "Deine AMD-Karte braucht für ROCm meist HSA_OVERRIDE_GFX_VERSION=$HSA. Für Ollama einrichten?" \
                              "Your AMD card usually needs HSA_OVERRIDE_GFX_VERSION=$HSA for ROCm. Set it up for Ollama?" y; then
  sudo mkdir -p /etc/systemd/system/ollama.service.d
  printf '[Service]\nEnvironment="HSA_OVERRIDE_GFX_VERSION=%s"\n' "$HSA" | \
    sudo tee /etc/systemd/system/ollama.service.d/orbwise-rocm.conf >/dev/null
  sudo systemctl daemon-reload && sudo systemctl restart ollama.service || true
fi

# ---------------------------------------------------------------- Python environment
say "Setting up the Python environment (uv)"
cd "$ROOT"
uv sync --extra voice

# ---------------------------------------------------------------- Model choice by VRAM
if [[ -z "$MODEL" ]]; then
  GB=0
  if [[ "$GPU" == "cuda" ]]; then
    MIB=$( { nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null || true; } | sort -n | tail -1)
    GB=$(( ${MIB:-0} / 1024 ))
  else
    for f in /sys/class/drm/card*/device/mem_info_vram_total; do
      [[ -r "$f" ]] || continue
      v=$(( $(cat "$f") / 1024 / 1024 / 1024 )); (( v > GB )) && GB=$v
    done
  fi
  [[ $GPU == cpu ]] && GB=0
  echo "$(t 'Grafikspeicher' 'Video memory'): ${GB} GB"
fi

say "$(t 'Sprachmodell wählen und laden (dauert beim ersten Mal)' 'Choosing and pulling the language model (takes a while the first time)')"
for _ in {1..30}; do ollama list >/dev/null 2>&1 && break; sleep 1; done
CHOICE_FILE="$(mktemp)"
OLLAMA_MODEL=""   # Ollama-Chatmodell
choose_model() {  # Menü geht ins Terminal, die Wahl in $CHOICE_FILE
  if (( ASK )); then uv run orbwise model choose --vram "$GB" --out "$CHOICE_FILE"
  else uv run orbwise model choose --vram "$GB" --out "$CHOICE_FILE" </dev/null; fi
}
while true; do
  if [[ -z "$MODEL" ]]; then choose_model; MODEL="$(cat "$CHOICE_FILE")"; fi
  echo "→ $MODEL"
  if ollama pull "$MODEL"; then
    OLLAMA_MODEL="$MODEL"
    break
  fi
  echo "$(t "✘ '$MODEL' konnte nicht eingerichtet werden." "✘ Could not set up '$MODEL'.")"
  if (( ASK )); then MODEL=""; else exit 1; fi
done
rm -f "$CHOICE_FILE"
ollama pull bge-m3   # Embeddings fürs Gedächtnis

# ---------------------------------------------------------------- Voice
say "$(t "Installiere die Sprachausgabe Pocket TTS" "Installing the Pocket TTS speech output")"
uv run orbwise voice install || echo "$(t "⚠ Pocket TTS nicht eingerichtet – später: orbwise voice install (bis dahin spricht der Browser)" \
  "⚠ Pocket TTS not set up – later: orbwise voice install (until then the browser speaks)")"

say "Downloading the wake word model and Whisper"
uv run python -c "import openwakeword.utils as u; u.download_models(['hey_jarvis'])"
uv run python -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8')"

# ---------------------------------------------------------------- Configuration
say "Configuration"
if [[ ! -f "$CONF/config.yaml" ]]; then
  uv run orbwise init-config
  [[ -n "$OLLAMA_MODEL" ]] && sed -i "s|^  model: .*|  model: $OLLAMA_MODEL|" "$CONF/config.yaml"
  sed -i "s|^language: [a-z]*|language: $LANG_CHOICE|" "$CONF/config.yaml"
else
  echo "$(t "$CONF/config.yaml gibt es schon – bleibt unverändert." "$CONF/config.yaml already exists – left unchanged.")"
fi

# ---------------------------------------------------------------- Skills (Mail, Paperless, Portainer …)
say "Skills"
if (( ASK )); then
  echo "$(t 'Orbwise kann sich mit deinen Diensten verbinden. Wähle aus, was du jetzt einrichten möchtest –
Zugangsdaten landen im Schlüsselbund, nicht im Klartext. Alles geht später auch unter Einstellungen → Skills.' \
            'Orbwise can connect to your services. Pick what you want to set up now – credentials go to the
keyring, not into a plain-text file. Everything also works later under Settings → Skills.')"
  uv run orbwise skills setup || echo "$(t '→ Später einrichten: orbwise skills setup' '→ Set up later: orbwise skills setup')"
else
  echo "$(t '→ Skills später einrichten: Einstellungen → Skills oder orbwise skills setup' \
            '→ Set up skills later: Settings → Skills or orbwise skills setup')"
fi

# ---------------------------------------------------------------- Launcher
say "Setting up the launcher"
mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications"
rm -f "$HOME/.local/bin/orbwise"
sed -e "s|@ORBWISE_HOME@|$ROOT|g" -e "s|@ORBWISE_BRANCH@|$BRANCH|g" -e "s|@ORBWISE_REPO@|$REPO|g" \
  "$ROOT/scripts/orbwise-launcher" > "$HOME/.local/bin/orbwise"
chmod 755 "$HOME/.local/bin/orbwise"

# Put ~/.local/bin on the PATH (once)
for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
  [[ -f "$rc" || "$rc" == "$HOME/.bashrc" ]] || continue
  if ! grep -q "# orbwise-path" "$rc" 2>/dev/null; then
    printf '\nexport PATH="$HOME/.local/bin:$PATH"  # orbwise-path\n' >> "$rc"
  fi
done
install -m 755 "$ROOT/scripts/orbwise-open" "$HOME/.local/bin/orbwise-open"
sed "s|@HOME@|$HOME|g" "$ROOT/scripts/orbwise.desktop" > "$HOME/.local/share/applications/orbwise.desktop"

if (( AUTOSTART )); then
  # Autostart inside the desktop session (needed to open files/apps on your desktop)
  mkdir -p "$HOME/.config/autostart"
  sed "s|@HOME@|$HOME|g" "$ROOT/scripts/orbwise-autostart.desktop" > "$HOME/.config/autostart/orbwise.desktop"
  echo "Autostart configured (~/.config/autostart/orbwise.desktop)."
fi

say "Done!"
cat <<EOF
Installed in: $ROOT
Start:    orbwise serve --open      (or "Orbwise" in the application menu)
Check:    orbwise doctor
Update:   orbwise update
(Open a new terminal or run "source ~/.bashrc" if "orbwise" is not found yet.)
Web UI:   orbwise open             (http://localhost:8765 – signs the browser in once)
Models:   orbwise model add        (download and switch to another model – also in the web UI: LLM menu)
EOF
echo "Skills:   orbwise skills        (Mail, Paperless, Portainer … – also under Settings → Skills)"
cat <<EOF

NVIDIA: speech recognition can run on the GPU – set voice.stt_device: cuda and voice.stt_compute_type: float16.
AMD: if ROCm does not pick up your card (ollama ps shows 100% CPU), see the README section "AMD GPUs".
EOF
