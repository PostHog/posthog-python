---
pypi/posthog: major
---

A `disable_geoip` argument to a call now counts as a value of that event. It wins over a `$geoip_disable` in context tags or `super_properties`, and a `$geoip_disable` in the call's own `properties` still wins over it. `disable_geoip=False` now sends `$geoip_disable: false`, so it turns GeoIP lookup on for that event even when a context tag or `super_properties` turns it off. The client's `disable_geoip` setting keeps its place below every caller value. `AsyncPosthog` follows the same rules.
