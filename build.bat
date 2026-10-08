@echo off
REM Genera dist\DriveMonitor\DriveMonitor.exe con PyInstaller.
REM Ejecutar desde la carpeta del proyecto, con Python 3.11+ instalado.
setlocal
python -m pip install --upgrade pip || goto :error
python -m pip install -r requirements-dev.txt || goto :error
python -m pytest -q || goto :error
python -m PyInstaller --noconfirm --clean --windowed --name DriveMonitor ^
  --exclude-module pyqtgraph.examples ^
  --collect-data tzdata --hidden-import tzdata ^
  run_drivemonitor.py || goto :error
echo.
echo Listo: dist\DriveMonitor\DriveMonitor.exe
goto :eof
:error
echo.
echo ERROR: revisa los mensajes de arriba.
exit /b 1
