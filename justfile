set shell := ["powershell.exe", "-NoLogo", "-NoProfile", "-Command"]

export UV_PROJECT_ENVIRONMENT := env_var_or_default("UV_PROJECT_ENVIRONMENT", env_var("USERPROFILE") + "/.venvs/ntero")
benchmark_library_root := env_var_or_default("NTERO_LIBRARY_ROOT", "C:/Users/richa/everquest/tex")
benchmark_texture_pack := env_var_or_default("NTERO_TEXTURE_PACK", "gigapixel-bc5")
benchmark_sound_pack := env_var_or_default("NTERO_SOUND_PACK", "quarm")
benchmark_game_dir := env_var_or_default("NTERO_GAME_DIR", "C:/Users/Public/Daybreak Game Company/Installed Games/EverQuest Legends")
benchmark_python := env_var_or_default("NTERO_BENCHMARK_PYTHON", "C:/Python314/python.exe")
benchmark_environment := env_var("USERPROFILE") + "/.venvs/ntero-pyspy"
benchmark_pack_duration := env_var_or_default("NTERO_PACK_PROFILE_SECONDS", "120")
benchmark_pack_rate := env_var_or_default("NTERO_PACK_PROFILE_HZ", "10")

default: test

test:
    uv run --extra dev pytest

coverage:
    uv run --extra dev pytest --cov=ntero --cov-report=term-missing --cov-fail-under=100

lint:
    uv run --extra dev pyupgrade --py314-plus --exit-zero-even-if-changed (Get-ChildItem src, tests -Recurse -Filter *.py).FullName
    uv run --extra dev autopep695 format src tests
    uv run --extra dev ssort src tests
    uv run --extra dev basedpyright
    uv run --extra dev ruff check --fix .
    uv run --extra dev ruff format .
    uv run --extra dev ruff check .
    uv run --extra dev pyrefly check --search-path .

[private]
benchmark-environment:
    Remove-Item Env:VIRTUAL_ENV -ErrorAction SilentlyContinue; $env:UV_PROJECT_ENVIRONMENT = "{{ benchmark_environment }}"; uv sync --python "{{ benchmark_python }}" --frozen --extra dev

benchmark-extract: benchmark-environment
    New-Item -ItemType Directory -Force profiles | Out-Null
    $env:PYTHONPATH = "$PWD/src;{{ benchmark_environment }}/Lib/site-packages"; uvx py-spy record --format flamegraph --output profiles/benchmark-extract.svg -- "{{ benchmark_python }}" -m ntero extract --library-root "{{ benchmark_library_root }}" --texture-pack-name "{{ benchmark_texture_pack }}" --sound-pack-name "{{ benchmark_sound_pack }}" --game-dir "{{ benchmark_game_dir }}" --benchmark

benchmark-update: benchmark-environment
    New-Item -ItemType Directory -Force profiles | Out-Null
    $env:PYTHONPATH = "$PWD/src;{{ benchmark_environment }}/Lib/site-packages"; uvx py-spy record --format flamegraph --output profiles/benchmark-update.svg -- "{{ benchmark_python }}" -m ntero update --library-root "{{ benchmark_library_root }}" --texture-pack-name "{{ benchmark_texture_pack }}" --sound-pack-name "{{ benchmark_sound_pack }}" --game-dir "{{ benchmark_game_dir }}" --benchmark

benchmark-pack: benchmark-environment
    New-Item -ItemType Directory -Force profiles | Out-Null
    $env:PYTHONPATH = "$PWD/src;{{ benchmark_environment }}/Lib/site-packages"; uvx py-spy record --native --rate "{{ benchmark_pack_rate }}" --duration "{{ benchmark_pack_duration }}" --format flamegraph --output profiles/benchmark-pack.svg -- "{{ benchmark_python }}" -m ntero pack --library-root "{{ benchmark_library_root }}" --texture-pack-name "{{ benchmark_texture_pack }}" --sound-pack-name "{{ benchmark_sound_pack }}" --benchmark

build:
    cargo build --release
    $work = Join-Path $env:TEMP "ntero-pyinstaller-$PID"; uv run --extra dev pyinstaller --clean --noconfirm --workpath $work build.spec; $code = $LASTEXITCODE; Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue; exit $code
