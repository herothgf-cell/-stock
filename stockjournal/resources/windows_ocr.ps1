# Local Windows.Media.Ocr bridge; compatible with Windows PowerShell 5.1.
# No network calls, credentials, or external OCR services are used.
param(
    [Parameter(Mandatory = $true)]
    [string] $ImagePathBase64,
    [Parameter(Mandatory = $true)]
    [string] $LanguageBase64
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding

$stream = $null
$bitmap = $null

try {
    $imagePath = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($ImagePathBase64))
    $languageTag = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($LanguageBase64))

    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
    $null = [Windows.Storage.FileAccessMode, Windows.Storage, ContentType = WindowsRuntime]
    $null = [Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
    $null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
    $null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Foundation, ContentType = WindowsRuntime]
    $null = [Windows.Graphics.Imaging.BitmapPixelFormat, Windows.Foundation, ContentType = WindowsRuntime]
    $null = [Windows.Graphics.Imaging.BitmapAlphaMode, Windows.Foundation, ContentType = WindowsRuntime]
    $null = [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]
    $null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
    $null = [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType = WindowsRuntime]

    $asTaskMethod = [System.WindowsRuntimeSystemExtensions].GetMethods() |
        Where-Object {
            $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and
            $_.GetGenericArguments().Count -eq 1 -and
            $_.GetParameters().Count -eq 1 -and
            $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
        } | Select-Object -First 1
    if ($null -eq $asTaskMethod) {
        throw 'The Windows Runtime async bridge is unavailable in this PowerShell installation.'
    }

    function Wait-WinRT {
        param([object] $Operation, [Type] $ResultType)
        $method = $asTaskMethod.MakeGenericMethod(@($ResultType))
        $task = $method.Invoke($null, @($Operation))
        $task.Wait()
        return $task.Result
    }

    $language = New-Object Windows.Globalization.Language($languageTag)
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
    if ($null -eq $engine) {
        $installed = @([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages | ForEach-Object { $_.LanguageTag }) -join ', '
        throw "Requested OCR language $languageTag is not installed. Install its Windows OCR language feature in Settings. Available languages: $installed"
    }

    $file = Wait-WinRT ([Windows.Storage.StorageFile]::GetFileFromPathAsync($imagePath)) ([Windows.Storage.StorageFile])
    $stream = Wait-WinRT ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $decoder = Wait-WinRT ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])

    $maximum = [Windows.Media.Ocr.OcrEngine]::MaxImageDimension
    if ($decoder.PixelWidth -gt $maximum -or $decoder.PixelHeight -gt $maximum) {
        throw "Image dimensions exceed the local OCR limit of $maximum pixels. Crop the trade area and try again."
    }
    $bitmap = Wait-WinRT ($decoder.GetSoftwareBitmapAsync(
        [Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8,
        [Windows.Graphics.Imaging.BitmapAlphaMode]::Premultiplied
    )) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $result = Wait-WinRT ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    $lines = @($result.Lines | ForEach-Object { $_.Text })
    [Console]::Out.Write(($lines -join [Environment]::NewLine))
}
catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
finally {
    if ($null -ne $bitmap) { $bitmap.Dispose() }
    if ($null -ne $stream) { $stream.Dispose() }
}
