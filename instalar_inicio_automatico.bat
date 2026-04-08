@echo off
echo Creando acceso directo en el inicio de Windows...
echo.

set SCRIPT_DIR=%~dp0
set STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup

:: Crear un archivo .vbs que inicia el bot minimizado
(
echo Set WshShell = CreateObject("WScript.Shell"^)
echo WshShell.Run "cmd /c cd /d ""%SCRIPT_DIR%"" && python cita_bot_playwright.py", 1, False
) > "%STARTUP_DIR%\CitaBotConsular.vbs"

echo.
echo Listo! El bot se va a iniciar automaticamente cuando prendas la PC.
echo Se creo: %STARTUP_DIR%\CitaBotConsular.vbs
echo.
echo Para desactivar el inicio automatico, borra ese archivo.
echo.
pause
