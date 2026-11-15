@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist "config.json" (
  copy "config.example.json" "config.json" >nul
  echo 已生成 config.json，project_root 留空表示使用 .\视频项目\
)

where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw "%~dp0app.py"
) else (
  start "" python "%~dp0app.py"
)
