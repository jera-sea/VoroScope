@echo off
setlocal enabledelayedexpansion

:: --- SETTINGS ---
set "HELICON_PATH=C:\Program Files\Helicon Software\Helicon Focus 8\HeliconFocus.exe"
set "DEST_FOLDER=T:\Processed_Stacks"
if not exist "%DEST_FOLDER%" mkdir "%DEST_FOLDER%"

:: Count total folders
set /a total=0
for /d %%i in (*) do set /a total+=1
set /a current=0

echo Found %total% stacks to process.
echo ---------------------------------

for /d %%G in (*) do (
    set /a current+=1
    title Processing: !current! of %total%
    echo [!current!/%total%] Processing folder: "%%G"

    :: We put the params BEFORE the folder path to ensure they are parsed correctly
    "%HELICON_PATH%" -silent -m:B -r:8 -s:4 "%%~fG" -save:"%DEST_FOLDER%\%%~nG.tif"
)

echo Done!
pause