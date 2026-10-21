@echo off
"C:\Program Files\Python313\python.exe" -m http.server 80 --directory D:\pxe\tftp --bind 192.168.77.1 >> D:\pxe\http80.log 2>&1
