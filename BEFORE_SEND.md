# Before Send Hook

The `before_send` parameter allows you to modify or filter events before they are sent to PostHog. This is useful for:

- **Privacy**: Removing or masking sensitive data (PII)
- **Filtering**: Dropping unwanted events (test events, internal users, etc.)
- **Enhancement**: Adding custom properties to all events
- **Transformation**: Modifying event names or property formats

## Basic Usage

```python
import posthog
from typing import Optional, Dict, Any

def my_before_send(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Process event before sending to PostHog.
    
    Args:
        event: The event dictionary containing 'event', 'distinct_id', 'properties', etc.
    
    Returns:
        Modified event dictionary to send, or None to drop the event
    """
    # Your processing logic here
    return event

# Initialize client with before_send hook
client = posthog.Client(
    api_key="your-project-api-key",
    before_send=my_before_send
)
```

## Common Use Cases

### 1. Filter Out Events

```python
from typing import Optional, Any

def filter_events_by_property_or_event_name(event: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Drop events from internal users or test environments."""
    properties = event.get("properties", {})
    
    # Choose some property from your events
    event_source = properties.get("event_source", "")
    if event_source.endswith("internal"):
        return None  # Drop the event
    
    # Filter out test events
    if event.get("event") == "test_event":
        return None
    
    return event
```

### 2. Remove/Mask PII Data

```python
from typing import Optional, Any

def scrub_pii(event: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Remove or mask personally identifiable information."""
    properties = event.get("properties", {})
    
    # Mask email but keep domain for analytics
    if "email" in properties:
        email = properties["email"]
        if "@" in email:
            domain = email.split("@")[1]
            properties["email"] = f"***@{domain}"
        else:
            properties["email"] = "***"
    
    # Remove sensitive fields entirely
    sensitive_fields = ["my_business_info", "secret_things"]
    for field in sensitive_fields:
        properties.pop(field, None)
    
    return event
```

### 3. Add Custom Properties

```python
from typing import Optional, Any

from datetime import datetime
from typing import Optional, Any

def add_context(event: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Add custom properties to all events."""
    if "properties" not in event:
        event["properties"] = {}
    
    event["properties"].update({
        "app_version": "2.1.0",
        "environment": "production", 
        "processed_at": datetime.now().isoformat()
    })
    
    return event
```

### 4. Transform Event Names

```python
from typing import Optional, Any

def normalize_event_names(event: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Convert event names to a consistent format."""
    original_event = event.get("event")
    if original_event:
        # Convert to snake_case
        normalized = original_event.lower().replace(" ", "_").replace("-", "_")
        event["event"] = f"app_{normalized}"
    
    return event
```

### 5. Log and drop in "dev" mode

When running in local dev often, you want to log but drop all events


```python
from typing import Optional, Any

def log_and_drop_all(event: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Convert event names to a consistent format."""
    print(event)
    
    return None
```

### 6. Combined Processing

```python
from typing import Optional, Any

def comprehensive_processor(event: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Apply multiple transformations in sequence."""
    
    # Step 1: Filter unwanted events
    if should_drop_event(event):
        return None
    
    # Step 2: Scrub PII
    event = scrub_pii(event)
    
    # Step 3: Add context
    event = add_context(event)
    
    # Step 4: Normalize names
    event = normalize_event_names(event)
    
    return event

def should_drop_event(event: dict[str, Any]) -> bool:
    """Determine if event should be dropped."""
    # Your filtering logic
    return False
```

## Error Handling

If your `before_send` function raises an exception, PostHog will:

1. Log the error 
2. Continue with the original, unmodified event
3. Not crash your application

```python
from typing import Optional, Any

def risky_before_send(event: dict[str, Any]) -> Optional[dict[str, Any]]:
    # If this raises an exception, the original event will be sent
    risky_operation()
    return event
```

## Complete Example

```python
import posthog
from typing import Optional, Any
import re

def production_before_send(event: dict[str, Any]) -> Optional[dict[str, Any]]:
    try:
        properties = event.get("properties", {})
        
        # 1. Filter out bot traffic
        user_agent = properties.get("$user_agent", "")
        if re.search(r'bot|crawler|spider', user_agent, re.I):
            return None
        
        # 2. Filter out internal traffic  
        ip = properties.get("$ip", "")
        if ip.startswith("192.168.") or ip.startswith("10."):
            return None
        
        # 3. Scrub email PII but keep domain
        if "email" in properties:
            email = properties["email"]
            if "@" in email:
                domain = email.split("@")[1]
                properties["email"] = f"***@{domain}"
        
        # 4. Add custom context
        properties.update({
            "app_version": "1.0.0",
            "build_number": "123"
        })
        
        # 5. Normalize event name
        if event.get("event"):
            event["event"] = event["event"].lower().replace(" ", "_")
        
        return event
        
    except Exception as e:
        # Log error but don't crash
        print(f"Error in before_send: {e}")
        return event  # Return original event on error

# Usage
client = posthog.Client(
    api_key="your-api-key",
    before_send=production_before_send
)

# All events will now be processed by your before_send function
client.capture("user_123", "Page View", {"url": "/home"})
```