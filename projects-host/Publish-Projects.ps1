param([string]$UserAccessBaseRelease = '', [string]$AuthBaseRelease = '', [string]$IndexBaseRelease = '', [string]$QRStudioBaseRelease = '', [string]$QRTransferBaseRelease = '', [string]$LampshadeBaseRelease = '', [string]$FloorBaseRelease = '', [string]$HomeAssistantBaseRelease = '', [string]$ImageReviewBaseRelease = '', [string]$TVRemoteBaseRelease = '', [string]$RemotesLayoutBaseRelease = '', [string]$RemotesUIBaseRelease = '', [string]$FloorInteractionBaseRelease = '', [string]$KartingBaseRelease = '', [string]$ActionLibraryBaseRelease = '', [string]$PartsBenchBaseRelease = '')
$ErrorActionPreference = 'Stop'
$taskPython = 'C:\Users\lamon\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$taskPublishArgs = @()
if ($PartsBenchBaseRelease) { $taskPublishArgs += @('--parts-bench-base-release', $PartsBenchBaseRelease) }
if ($ActionLibraryBaseRelease) { $taskPublishArgs += @('--action-library-base-release', $ActionLibraryBaseRelease) }
if ($KartingBaseRelease) { $taskPublishArgs += @('--karting-base-release', $KartingBaseRelease) }
if ($FloorInteractionBaseRelease) { $taskPublishArgs += @('--floor-interaction-base-release', $FloorInteractionBaseRelease) }
if ($RemotesUIBaseRelease) { $taskPublishArgs += @('--remotes-ui-base-release', $RemotesUIBaseRelease) }
if ($RemotesLayoutBaseRelease) { $taskPublishArgs += @('--remotes-layout-base-release', $RemotesLayoutBaseRelease) }
if ($TVRemoteBaseRelease) { $taskPublishArgs += @('--tv-remote-base-release', $TVRemoteBaseRelease) }
if ($UserAccessBaseRelease) { $taskPublishArgs += @('--user-access-base-release', $UserAccessBaseRelease) }
if ($AuthBaseRelease) { $taskPublishArgs += @('--auth-base-release', $AuthBaseRelease) }
if ($IndexBaseRelease) { $taskPublishArgs += @('--index-base-release', $IndexBaseRelease) }
if ($QRStudioBaseRelease) { $taskPublishArgs += @('--qr-studio-base-release', $QRStudioBaseRelease) }
if ($QRTransferBaseRelease) { $taskPublishArgs += @('--qr-transfer-base-release', $QRTransferBaseRelease) }
if ($LampshadeBaseRelease) { $taskPublishArgs += @('--lampshade-base-release', $LampshadeBaseRelease) }
if ($FloorBaseRelease) { $taskPublishArgs += @('--floor-base-release', $FloorBaseRelease) }
if ($HomeAssistantBaseRelease) { $taskPublishArgs += @('--home-assistant-base-release', $HomeAssistantBaseRelease) }
if ($ImageReviewBaseRelease) { $taskPublishArgs += @('--image-review-base-release', $ImageReviewBaseRelease) }
& $taskPython (Join-Path $PSScriptRoot 'publish.py') @taskPublishArgs
if ($LASTEXITCODE -ne 0) { throw 'Projects were not published. See the error above.' }
