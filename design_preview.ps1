param([string]$In, [string]$OutPrefix, [int]$Width = 2400, [int]$MaxPages = 8)
# Design (.ai / .pdf) pages -> JPG previews, Windows built-in PDF renderer (Windows.Data.Pdf).
# Page 1 -> <OutPrefix>.jpg, page n -> <OutPrefix>-p<n>.jpg (up to MaxPages). Long side = Width px.
# Called by design_preview.py on the local PC. ASCII only (PowerShell 5.1 misreads non-BOM UTF-8).
$ErrorActionPreference = 'Stop'
try {
  Add-Type -AssemblyName System.Runtime.WindowsRuntime
  $ms = [System.WindowsRuntimeSystemExtensions].GetMethods()
  $asOp = ($ms | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
  $asAct = ($ms | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncAction' })[0]
  function Await($op, $type) { $t = $asOp.MakeGenericMethod($type).Invoke($null, @($op)); $t.Wait(); $t.Result }
  function AwaitAction($op) { $t = $asAct.Invoke($null, @($op)); $t.Wait() }
  [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
  [Windows.Storage.StorageFolder, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
  [Windows.Data.Pdf.PdfDocument, Windows.Data.Pdf, ContentType = WindowsRuntime] | Out-Null
  [Windows.Data.Pdf.PdfPageRenderOptions, Windows.Data.Pdf, ContentType = WindowsRuntime] | Out-Null
  [Windows.Graphics.Imaging.BitmapEncoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime] | Out-Null
  $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($In)) ([Windows.Storage.StorageFile])
  $doc = Await ([Windows.Data.Pdf.PdfDocument]::LoadFromFileAsync($file)) ([Windows.Data.Pdf.PdfDocument])
  $folder = Await ([Windows.Storage.StorageFolder]::GetFolderFromPathAsync((Split-Path -Parent $OutPrefix))) ([Windows.Storage.StorageFolder])
  $leaf = Split-Path -Leaf $OutPrefix
  $n = [Math]::Min([int]$doc.PageCount, $MaxPages)
  for ($i = 0; $i -lt $n; $i++) {
    $page = $doc.GetPage([uint32]$i)
    $opt = New-Object Windows.Data.Pdf.PdfPageRenderOptions
    if ($page.Size.Width -ge $page.Size.Height) { $opt.DestinationWidth = [uint32]$Width } else { $opt.DestinationHeight = [uint32]$Width }
    $opt.BitmapEncoderId = [Windows.Graphics.Imaging.BitmapEncoder]::JpegEncoderId
    if ($i -eq 0) { $name = "$leaf.jpg" } else { $name = "$leaf-p$($i + 1).jpg" }
    $f2 = Await ($folder.CreateFileAsync($name, [Windows.Storage.CreationCollisionOption]::ReplaceExisting)) ([Windows.Storage.StorageFile])
    $stream = Await ($f2.OpenAsync([Windows.Storage.FileAccessMode]::ReadWrite)) ([Windows.Storage.Streams.IRandomAccessStream])
    AwaitAction ($page.RenderToStreamAsync($stream, $opt))
    $stream.Dispose()
    $page.Dispose()
  }
  Write-Output ('{"ok": true, "pages": ' + $doc.PageCount + ', "rendered": ' + $n + '}')
} catch {
  $m = ($_.Exception.Message -replace '[\r\n"\\]', ' ')
  Write-Output ('{"ok": false, "error": "' + $m.Substring(0, [Math]::Min(250, $m.Length)) + '"}')
}
