@echo off
rem httpserve.py instead of http.server: ks is only served to the machine unlocked on the cart (none on Windows, so rescue only)
"C:\Program Files\Python313\python.exe" D:\pxe\httpserve.py 8000 --directory D:\pxe\http --bind 192.0.2.1 --pxe-mode D:\pxe\pxe-mode.json >> D:\pxe\http.log 2>&1
