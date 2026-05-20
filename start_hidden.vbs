Dim ws
Set ws = CreateObject("WScript.Shell")
ws.CurrentDirectory = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
ws.Run "cmd /c pip install flask flask-socketio requests websockets streamlink --quiet", 0, True
ws.Run "python """ & ws.CurrentDirectory & "app.py""", 0, False
