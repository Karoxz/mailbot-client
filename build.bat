@echo off
echo Building MailBot Dispatcher EXE...
 
python -m nuitka ^
  --standalone ^
  --onefile ^
  --windows-console-mode=disable ^
  --windows-icon-from-ico=assets\plutus_logo.ico ^
  --output-filename=PlutusBotDispatcher ^
  --output-dir=dist ^
  --include-data-dir=assets=assets ^
  --include-package=google ^
  --include-package=googleapiclient ^
  --include-package=httplib2 ^
  --include-package=requests ^
  --include-package=PIL ^
  --enable-plugin=tk-inter ^
  --assume-yes-for-downloads ^
  main.py

echo Done! EXE is at dist\PlutusBotDispatcher.exe
pause
