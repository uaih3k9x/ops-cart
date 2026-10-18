@echo off
"C:\Program Files\Python313\python.exe" -m http.server 8000 --directory D:\pxe\http --bind 192.168.200.1 >> D:\pxe\http.log 2>&1
