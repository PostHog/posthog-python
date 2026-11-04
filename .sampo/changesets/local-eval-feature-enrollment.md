---
pypi/posthog: patch
---

Local evaluation now respects early access feature enrollment. A flag with feature enrollment uses the `$feature_enrollment/<flag key>` person property when you pass it, and otherwise falls back to the flags service. Before, it returned `False` for enrolled people who matched no release condition, while the flags service returned `True`.
