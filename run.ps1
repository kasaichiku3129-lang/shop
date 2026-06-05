# 0513 伝票 AI 読み取り（ポート 8513。.streamlit/config.toml 参照）
Set-Location $PSScriptRoot
if (Test-Path ".\.venv311\Scripts\Activate.ps1") {
    . .\.venv311\Scripts\Activate.ps1
} elseif (Test-Path ".\.venv\Scripts\Activate.ps1") {
    . .\.venv\Scripts\Activate.ps1
}
streamlit run app.py
