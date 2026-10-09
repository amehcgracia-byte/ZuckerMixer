ZUCKER MIXER — LÉEME PRIMERO / READ ME FIRST
=============================================

(English below)


ESPAÑOL
-------

1. INSTALAR
   Arrastra ZuckerMixer.app a la carpeta Aplicaciones.

2. LA PRIMERA VEZ, macOS LA BLOQUEARÁ (ES NORMAL)
   Al abrirla verás un aviso parecido a:
   "No se ha abierto ZuckerMixer. Apple no ha podido verificar que
   ZuckerMixer no contiene software malicioso..."

   No es un virus ni un fallo. Ocurre porque Zucker Mixer es una app
   gratuita y no hemos pagado la cuota de desarrollador de Apple
   (99 €/año), así que Apple no la ha "notarizado". Tus grabaciones no
   salen de tu ordenador. La app solo se conecta a internet para buscar
   actualizaciones en GitHub y, la primera vez que detecta canciones,
   para descargar el modelo de voz Whisper.

   Para abrirla (solo hace falta una vez):
     a) En el aviso, pulsa "Aceptar" (NO "Trasladar a la papelera").
     b) Abre  Ajustes del Sistema  >  Privacidad y seguridad.
     c) Baja hasta la sección "Seguridad". Verás:
        "Se ha bloqueado ZuckerMixer para proteger tu Mac."
     d) Pulsa  "Abrir igualmente"  y escribe tu contraseña del Mac.
     e) En el último aviso, pulsa otra vez "Abrir igualmente".

   A partir de ahí se abre normalmente, con doble clic.
   Las actualizaciones automáticas no vuelven a pedir este paso.

   En macOS 14 (Sonoma) o anterior también vale:
   clic derecho sobre ZuckerMixer.app  >  Abrir  >  Abrir.

   Plan B (Terminal), si lo anterior no aparece:
     xattr -dr com.apple.quarantine /Applications/ZuckerMixer.app

3. MAC CON CHIP APPLE (M1, M2, M3...)
   Si macOS pide instalar Rosetta, acepta. La app está hecha para Intel
   y Rosetta permite usarla en estos Mac.

4. FFMPEG (para crear los MP3)
   Si la app dice que falta ffmpeg, instálalo en Terminal:
     brew install ffmpeg

5. TUS ARCHIVOS
   Todo se queda en este Mac. Ajustes e informes de diagnóstico:
     ~/Music/JamMixes/ZuckerMixerState

Zucker Mixer es software libre bajo la licencia GNU GPL v3.0
(ver LICENSE.txt). Copyright (C) 2026 José Manuel García.


ENGLISH
-------

1. INSTALL
   Drag ZuckerMixer.app into the Applications folder.

2. THE FIRST TIME, macOS WILL BLOCK IT (THIS IS EXPECTED)
   You will see a warning like:
   "ZuckerMixer Not Opened. Apple could not verify ZuckerMixer is free
   of malware..."

   This is not a virus or a bug. Zucker Mixer is free and we have not
   paid Apple's developer fee (99 USD/year), so Apple has not
   "notarized" it. Your recordings never leave your computer. The app
   only goes online to check GitHub for updates and, the first time it
   detects songs, to download the Whisper speech model.

   To open it (only needed once):
     a) In the warning, click "Done" (NOT "Move to Trash").
     b) Open  System Settings  >  Privacy & Security.
     c) Scroll down to "Security". You will see:
        "ZuckerMixer was blocked to protect your Mac."
     d) Click  "Open Anyway"  and enter your Mac password.
     e) In the final prompt, click "Open Anyway" again.

   After that it opens normally with a double-click.
   Automatic updates do not require this step again.

   On macOS 14 (Sonoma) or earlier you can also:
   right-click ZuckerMixer.app  >  Open  >  Open.

   Plan B (Terminal), if the option above does not appear:
     xattr -dr com.apple.quarantine /Applications/ZuckerMixer.app

3. APPLE SILICON MACS (M1, M2, M3...)
   If macOS asks to install Rosetta, accept. The app is built for Intel
   and Rosetta runs it on these Macs.

4. FFMPEG (needed to create MP3 files)
   If the app says ffmpeg is missing, install it in Terminal:
     brew install ffmpeg

5. YOUR FILES
   Everything stays on this Mac. Settings and diagnostic reports:
     ~/Music/JamMixes/ZuckerMixerState

Zucker Mixer is free software licensed under the GNU GPL v3.0
(see LICENSE.txt). Copyright (C) 2026 José Manuel García.
