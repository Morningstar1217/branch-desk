Add-Type -AssemblyName System.Drawing
$images = @()
foreach ($size in @(16,24,32,48,64,128,256)) {
    $bitmap = [System.Drawing.Bitmap]::new($size,$size)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $graphics.Clear([System.Drawing.Color]::Transparent)
    $graphics.ScaleTransform(($size/32.0),($size/32.0))
    $shape = [System.Drawing.Drawing2D.GraphicsPath]::new()
    $shape.AddArc(1,1,16,16,180,90); $shape.AddArc(15,1,16,16,270,90)
    $shape.AddArc(15,15,16,16,0,90); $shape.AddArc(1,15,16,16,90,90); $shape.CloseFigure()
    $background = [System.Drawing.SolidBrush]::new([System.Drawing.ColorTranslator]::FromHtml('#123044'))
    $graphics.FillPath($background,$shape)
    $pen = [System.Drawing.Pen]::new([System.Drawing.ColorTranslator]::FromHtml('#8be6dd'),2.3)
    $pen.StartCap = $pen.EndCap = [System.Drawing.Drawing2D.LineCap]::Round
    $graphics.DrawLine($pen,10,11,10,21)
    $branch = [System.Drawing.Drawing2D.GraphicsPath]::new()
    $branch.AddBezier(22,11,22,18,18,18,10,18)
    $graphics.DrawPath($pen,$branch)
    $graphics.DrawEllipse($pen,7,5,6,6); $graphics.DrawEllipse($pen,19,5,6,6); $graphics.DrawEllipse($pen,7,21,6,6)
    # Write 32-bit BGRA DIBs directly; Icon.FromHandle.Save quantizes to 4-bit.
    $memory=[System.IO.MemoryStream]::new();$dib=[System.IO.BinaryWriter]::new($memory)
    $maskStride=[int]([Math]::Ceiling($size/32.0)*4)
    $dib.Write([uint32]40);$dib.Write([int]$size);$dib.Write([int]($size*2))
    $dib.Write([uint16]1);$dib.Write([uint16]32);$dib.Write([uint32]0);$dib.Write([uint32]($size*$size*4))
    1..4 | ForEach-Object {$dib.Write([uint32]0)}
    for($y=$size-1;$y -ge 0;$y--){for($x=0;$x -lt $size;$x++){$pixel=$bitmap.GetPixel($x,$y);$dib.Write($pixel.B);$dib.Write($pixel.G);$dib.Write($pixel.R);$dib.Write($pixel.A)}}
    for($y=$size-1;$y -ge 0;$y--){$mask=[byte[]]::new($maskStride);for($x=0;$x -lt $size;$x++){if($bitmap.GetPixel($x,$y).A -eq 0){$mask[[int][Math]::Floor($x/8)]=$mask[[int][Math]::Floor($x/8)] -bor (128 -shr ($x%8))}};$dib.Write($mask)}
    $dib.Flush();$images += ,@{Size=$size;Data=$memory.ToArray()}
    $memory.Dispose();$branch.Dispose();$shape.Dispose();$pen.Dispose();$background.Dispose();$graphics.Dispose();$bitmap.Dispose()
}
$file=[System.IO.File]::Create((Join-Path $PSScriptRoot 'BranchDesk.ico'))
$writer=[System.IO.BinaryWriter]::new($file)
$writer.Write([uint16]0);$writer.Write([uint16]1);$writer.Write([uint16]$images.Count)
$position=6+16*$images.Count
foreach($entry in $images){$dimension=if($entry.Size -eq 256){0}else{$entry.Size};$writer.Write([byte]$dimension);$writer.Write([byte]$dimension);$writer.Write([byte]0);$writer.Write([byte]0);$writer.Write([uint16]1);$writer.Write([uint16]32);$writer.Write([uint32]$entry.Data.Length);$writer.Write([uint32]$position);$position+=$entry.Data.Length}
foreach($entry in $images){$writer.Write([byte[]]$entry.Data)}
$writer.Dispose();$file.Dispose()
