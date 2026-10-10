$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName System.Runtime.WindowsRuntime
[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null
[Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime] | Out-Null
[Windows.Globalization.Language, Windows.Globalization, ContentType=WindowsRuntime] | Out-Null
$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetGenericArguments().Count -eq 1 -and
    $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
} | Select-Object -First 1
function Await-WinRT($operation, $resultType) {
    $task = $asTask.MakeGenericMethod($resultType).Invoke($null, @($operation))
    $task.Wait()
    return $task.Result
}
$language = New-Object Windows.Globalization.Language('zh-Hans-CN')
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
if ($null -eq $engine) { throw 'Windows Chinese OCR is unavailable.' }
[Console]::WriteLine('{"ready":true}')
while ($null -ne ($request = [Console]::ReadLine())) {
    $stream = $null
    $bitmap = $null
    try {
        $inputData = $request | ConvertFrom-Json
        $file = Await-WinRT ([Windows.Storage.StorageFile]::GetFileFromPathAsync($inputData.path)) ([Windows.Storage.StorageFile])
        $stream = Await-WinRT ($file.OpenReadAsync()) ([Windows.Storage.Streams.IRandomAccessStreamWithContentType])
        $decoder = Await-WinRT ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Await-WinRT ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $result = Await-WinRT ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
        $lines = @($result.Lines | ForEach-Object {
            $words = @($_.Words)
            $left = ($words | ForEach-Object { $_.BoundingRect.X } | Measure-Object -Minimum).Minimum
            $top = ($words | ForEach-Object { $_.BoundingRect.Y } | Measure-Object -Minimum).Minimum
            $right = ($words | ForEach-Object { $_.BoundingRect.X + $_.BoundingRect.Width } | Measure-Object -Maximum).Maximum
            $bottom = ($words | ForEach-Object { $_.BoundingRect.Y + $_.BoundingRect.Height } | Measure-Object -Maximum).Maximum
            @{ text = $_.Text; x = $left; y = $top; w = $right - $left; h = $bottom - $top }
        })
        $response = @{ text = $result.Text; lines = $lines } | ConvertTo-Json -Depth 5 -Compress
    } catch {
        $response = @{ error = $_.Exception.Message } | ConvertTo-Json -Compress
    } finally {
        if ($null -ne $bitmap) { $bitmap.Dispose() }
        if ($null -ne $stream) { $stream.Dispose() }
    }
    [Console]::WriteLine($response)
}
