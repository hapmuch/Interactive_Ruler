def classFactory(iface):
    from .interactive_ruler import InteractiveRulerPlugin
    return InteractiveRulerPlugin(iface)