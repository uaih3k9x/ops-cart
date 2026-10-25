@echo off
"C:\Program Files\Python313\python.exe" D:\pxe\httpserve.py 80 --directory D:\pxe\tftp --bind 192.0.2.1 --pxe-mode D:\pxe\pxe-mode.json >> D:\pxe\http80.log 2>&1
