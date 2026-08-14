# Fish Audio Desktop — compilación automática

Este repositorio está preparado para que GitHub compile el programa en un runner Windows y entregue:

- `FishAudioDesktop.exe`
- `FishAudioDesktop-Setup.exe`

No necesitas Python en tu PC para obtener el ejecutable.

## Pasos

1. Crea un repositorio nuevo en GitHub, por ejemplo `FishAudioDesktop`.
2. Sube todos los archivos de esta carpeta manteniendo `.github/workflows/build-windows.yml`.
3. En GitHub abre **Actions**.
4. Selecciona **Build Fish Audio Desktop for Windows**.
5. Pulsa **Run workflow**.
6. Cuando termine en verde, abre la ejecución.
7. En **Artifacts** descarga `FishAudioDesktop-Windows-Installer`.
8. Dentro encontrarás `FishAudioDesktop-Setup.exe`.

GitHub Actions puede ejecutar el trabajo en una máquina virtual Windows y guardar el `.exe` como artifact para descargarlo.
