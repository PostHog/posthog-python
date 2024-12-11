from posthog.client import Client

class PostHogFactory:
    @staticmethod
    def create(
        api_key=None,
        host=None,
        **kwargs
    ):
        """
        Create a new PostHog client instance or return the existing one.
        """
        return Client(api_key=api_key, host=host, **kwargs)
    
    @staticmethod
    def get_instance():
        """
        Get the existing PostHog client instance.
        """
        return Client.get_instance()
