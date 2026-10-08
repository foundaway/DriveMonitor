@echo off
REM Genera dist\DriveMonitor.exe: un solo archivo, sin carpetas adicionales.
REM Ejecutar desde la carpeta del proyecto, con Python 3.11+ instalado.
setlocal
python -m pip install --upgrade pip || goto :error
python -m pip install -r requirements-dev.txt || goto :error
python -m pytest -q || goto :error
python -m PyInstaller --noconfirm --clean --onefile --windowed --name DriveMonitor ^
  --exclude-module pyqtgraph.examples ^
  --collect-data tzdata --hidden-import tzdata ^
  run_drivemonitor.py || goto :error
REM La carpeta build\ y el archivo .spec son temporales.
rmdir /s /q build 2>nul
del /q DriveMonitor.spec 2>nul
echo.
echo Listo: dist\DriveMonitor.exe  (es el unico archivo que necesitas)
goto :eof
:error
echo.
echo ERROR: revisa los mensajes de arriba.
exit /b 1
