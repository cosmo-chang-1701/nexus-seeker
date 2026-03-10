
import sys
import os
import pandas as pd
from typing import Dict

# Add project root to sys.path
sys.path.append(os.getcwd())

try:
    from nexus_core.services.market_data_service import get_macro_environment
    
    print("Testing get_macro_environment()...")
    result = get_macro_environment()
    print(f"Result: {result}")
    
    # Basic validation
    assert isinstance(result, dict)
    assert "vix" in result
    assert "oil" in result
    assert "vix_change" in result
    print("Test passed!")
except Exception as e:
    print(f"Test failed with error: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
