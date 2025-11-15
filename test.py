import os
import re
from datetime import datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from posthog import Posthog

class CustomReprClass:
    def __repr__(self):
        return '<CustomReprClass: custom representation>'

class CustomObject:
    def __init__(self, value):
        self.value = value
    
    def __repr__(self):
        return f'CustomObject(value={self.value})'

class CircularRef:
    def __init__(self):
        self.ref = self
    
    def __repr__(self):
        return '<CircularRef with self-reference>'

posthog = Posthog(
    "phc_J1o2BXYxzXBHJeG2mS5hk62ijkTWk38Z385lO0MhU5w",
    host="http://localhost:8010",
    debug=True,
    enable_exception_autocapture=True,
    capture_exception_code_variables=True,
    project_root=os.path.dirname(os.path.abspath(__file__))
)

def trigger_error():
    # Variables that can't be JSON-serialized but have useful repr()
    my_regex = re.compile(r'\d+')
    my_datetime = datetime(2024, 1, 15, 10, 30, 45)
    my_timedelta = timedelta(days=5, hours=3)
    my_decimal = Decimal('123.456')
    my_fraction = Fraction(3, 4)
    my_set = {1, 2, 3}
    my_frozenset = frozenset([4, 5, 6])
    my_bytes = b'hello bytes'
    my_bytearray = bytearray(b'mutable bytes')
    my_memoryview = memoryview(b'memory view')
    my_complex = complex(3, 4)
    my_range = range(10)
    my_custom = CustomReprClass()
    my_obj = CustomObject(42)
    my_circular = CircularRef()
    my_lambda = lambda x: x * 2
    my_function = trigger_error
    
    1/0

trigger_error()
