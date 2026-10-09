"""Load scientific DSP libraries on first use, after the interface can open."""
class LazyDspModule:
    def __init__(self,name):
        self.name=name
        self.module=None

    def __getattr__(self,attribute):
        if self.module is None:
            # Literal imports keep PyInstaller dependency hooks complete.
            if self.name=='signal':
                from scipy import signal as loaded
            elif self.name=='ndimage':
                from scipy import ndimage as loaded
            else:
                import pyloudnorm as loaded
            self.module=loaded
        return getattr(self.module,attribute)

signal=LazyDspModule('signal')
ndimage=LazyDspModule('ndimage')
pyloudnorm=LazyDspModule('pyloudnorm')
