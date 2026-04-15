Set sh = CreateObject("WScript.Shell")
root = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\") - 1)
sh.CurrentDirectory = root
sh.Run "python src\installer\darkthrone_app.py", 0, False
