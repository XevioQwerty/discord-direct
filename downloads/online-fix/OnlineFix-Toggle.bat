@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ============================================================
REM   Universal Online-Fix Toggle   -   works with ANY game
REM ------------------------------------------------------------
REM  Why: Online-Fix multiplayer is a separate P2P network, so
REM  EVERYONE in the lobby must run the same fix - including
REM  people who own the real game. This lets a legit owner flip
REM  their copy between real Steam and the Online-Fix in 1 click.
REM
REM  ONE-TIME SETUP:
REM    1. Put this .bat in your game's root folder (where the
REM       game's .exe lives).
REM    2. Make a subfolder named  _OnlineFix  and extract your
REM       Online-Fix .zip into it (the files meant for the root).
REM    3. Double-click this .bat -> press  S  to create shortcuts.
REM
REM  THEN just use the desktop shortcuts:
REM    "<Game> (Online Fix)"  applies the fix, launches the game
REM    "<Game> (Normal)"      restores originals, launches game
REM ============================================================

set "GAMEDIR=%~dp0"
set "FIXDIR=%GAMEDIR%_OnlineFix"
set "BACKUP=%GAMEDIR%_OnlineFix_Backup"
set "CFG=%GAMEDIR%_OnlineFix.cfg"
set "MARK=%BACKUP%\.active"
for %%I in ("%GAMEDIR:~0,-1%") do set "GNAME=%%~nxI"

set "GAME_EXE="
if exist "%CFG%" set /p GAME_EXE=<"%CFG%"

set "MODE=%~1"
if /I "%MODE%"=="ON"      ( call :enable  & call :launch & goto :end )
if /I "%MODE%"=="OFF"     ( call :disable & call :launch & goto :end )
if /I "%MODE%"=="ENABLE"  ( call :enable  & goto :end )
if /I "%MODE%"=="DISABLE" ( call :disable & goto :end )
if /I "%MODE%"=="SETUP"   ( call :setup   & goto :end )

:menu
echo.
echo    ===  Online-Fix Toggle  -  %GNAME%  ===
if exist "%MARK%" (echo    Current state:  ONLINE FIX IS ON) else (echo    Current state:  ONLINE FIX IS OFF)
echo.
echo      [1]  Turn Online Fix ON
echo      [2]  Turn Online Fix OFF
echo      [S]  Create desktop shortcuts
echo      [Q]  Quit
echo.
set /p "CH=Choose: "
if /I "%CH%"=="1" ( call :enable  & pause & goto :end )
if /I "%CH%"=="2" ( call :disable & pause & goto :end )
if /I "%CH%"=="S" ( call :setup   & goto :end )
goto :end

:enable
if not exist "%FIXDIR%" ( echo [!] No _OnlineFix folder. Extract your online-fix zip into "%FIXDIR%". & exit /b 1 )
if exist "%MARK%" ( echo Online Fix is already ON. & exit /b 0 )
if not exist "%BACKUP%" mkdir "%BACKUP%"
for %%F in ("%FIXDIR%\*") do (
    if exist "%GAMEDIR%%%~nxF" copy /Y "%GAMEDIR%%%~nxF" "%BACKUP%\" >nul
    copy /Y "%%F" "%GAMEDIR%" >nul
)
break>"%MARK%"
echo Online Fix: ENABLED
exit /b 0

:disable
if not exist "%MARK%" ( echo Online Fix is already OFF. & exit /b 0 )
for %%F in ("%FIXDIR%\*") do (
    if exist "%BACKUP%\%%~nxF" (
        copy /Y "%BACKUP%\%%~nxF" "%GAMEDIR%" >nul
    ) else (
        if exist "%GAMEDIR%%%~nxF" del /Q "%GAMEDIR%%%~nxF"
    )
)
del /Q "%MARK%" >nul 2>&1
echo Online Fix: DISABLED
exit /b 0

:launch
if "%GAME_EXE%"=="" ( echo [!] No game exe set. Run setup ^(option S^) first. & pause & exit /b 1 )
if not exist "%GAMEDIR%%GAME_EXE%" ( echo [!] Cannot find "%GAME_EXE%" in this folder. & pause & exit /b 1 )
start "" "%GAMEDIR%%GAME_EXE%"
exit /b 0

:setup
echo.
if "%GAME_EXE%"=="" (
    echo Enter the game's EXE file name exactly as it appears here.
    echo Example:  ForzaHorizon5.exe
    set /p "GAME_EXE=Game EXE: "
    >"%CFG%" echo !GAME_EXE!
)
echo Using game exe: !GAME_EXE!
set "DESK=%USERPROFILE%\Desktop"
set "SELF=%~f0"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
 "$W=New-Object -ComObject WScript.Shell;" ^
 "$a=$W.CreateShortcut('%DESK%\%GNAME% (Online Fix).lnk');$a.TargetPath='%SELF%';$a.Arguments='ON';$a.WorkingDirectory='%GAMEDIR%';$a.WindowStyle=7;$a.Save();" ^
 "$b=$W.CreateShortcut('%DESK%\%GNAME% (Normal).lnk');$b.TargetPath='%SELF%';$b.Arguments='OFF';$b.WorkingDirectory='%GAMEDIR%';$b.WindowStyle=7;$b.Save();"
echo.
echo Created desktop shortcuts:
echo    - %GNAME% (Online Fix)
echo    - %GNAME% (Normal)
pause
exit /b 0

:end
endlocal
