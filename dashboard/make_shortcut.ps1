# 创建 learn-coach 桌面快捷方式（由 make_shortcut.ps1 调用，UTF-8 BOM）
$ErrorActionPreference = 'Stop'
$ws = New-Object -ComObject WScript.Shell
$desk = [Environment]::GetFolderPath('Desktop')

# 清理可能的旧/乱码快捷方式
Get-ChildItem $desk -Filter '*.lnk' | Where-Object {
    $_.BaseName -like '*learn-coach*' -or $_.BaseName -like '*ѧϰ*'
} | Remove-Item -ErrorAction SilentlyContinue

$lnkPath = Join-Path $desk 'learn-coach 学习系统.lnk'
$lnk = $ws.CreateShortcut($lnkPath)
$lnk.TargetPath = 'C:\代码文件夹\learn-coach\学习系统.vbs'
$lnk.WorkingDirectory = 'C:\代码文件夹\learn-coach'
$lnk.IconLocation = 'C:\代码文件夹\learn-coach\dashboard\assets\app.ico'
$lnk.Description = 'AI 学习教练：资料进、证书出'
$lnk.Save()
Write-Output ('OK: ' + $lnkPath)

# 顺带检查 Edge 应用窗口进程
$edge = Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" |
    Where-Object { $_.CommandLine -match '--app=http://127\.0\.0\.1:8765' } |
    Select-Object -First 1
if ($edge) { Write-Output 'EDGE-APP: running' } else { Write-Output 'EDGE-APP: not found' }
