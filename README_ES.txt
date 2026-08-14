FISH AUDIO DESKTOP
==================

Cliente de escritorio no oficial para Windows 11 que usa la API oficial de Fish Audio.

Incluye:
- Texto a voz (TTS)
- Selección de modelo
- reference_id para voces existentes
- Prosodia: velocidad y volumen
- Parámetros avanzados: temperature, top_p, chunk_length, latency
- MP3/WAV/Opus/PCM según lo admitido por la API
- Speech-to-Text (ASR)
- Gestión de modelos: listar, crear y eliminar
- Clonación mediante archivos de referencia
- Exportación local de audio
- Interfaz oscura compatible con Windows 11

IMPORTANTE:
No es una copia del sitio fish.audio ni incluye sus voces/datos privados. Usa tu propia cuenta y API key.
La API key se introduce localmente en la app. No la compartas.

CÓMO CREAR EL EXE EN WINDOWS 11
1. Instala Python 3.11+ desde python.org y marca "Add Python to PATH".
2. Ejecuta build_windows.bat.
3. El ejecutable aparecerá en dist\FishAudioDesktop.exe.

CÓMO CREAR EL INSTALADOR
1. Instala Inno Setup 6.
2. Ejecuta build_windows.bat.
3. Abre FishAudioDesktop.iss con Inno Setup y pulsa Compile.
4. El instalador aparecerá en la carpeta installer.

NOTA SOBRE "TODO LO QUE TIENE":
Fish Audio cambia y amplía su API. Este proyecto cubre las funciones principales expuestas oficialmente (TTS, ASR, modelos/voz y clonación). Funciones web específicas de la plataforma, como su feed social, biblioteca completa, pagos, cuenta y componentes internos, no se duplican dentro de un cliente de escritorio independiente.
