"""Phase H — execution layer (the ONLY place broker order APIs may be called).

    Signal -> Risk Engine -> Safety Gates -> Execution Gate -> Dhan Adapter

Default state: RESEARCH mode, live execution DISABLED and DISARMED.
"""
