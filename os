debug=debug,
            


 feature_flags_request_timeout_seconds=feature_flags_request_timeout_seconds,
            
            super_properties=super_properties


on_error=on_error,
            send=send,
            sync_mode=sync_mode,
            personal_api_key=personal_api_key,
            poll_interval=poll_interval,
            disabled=disabled,
            disable_geoip=disable_geoip,
            is_server=is_server,
           ,
            # TODO: Currently this monitoring begins only when the Client is initialised (which happens when you do something with the SDK)
            # This kind of initialisation is very annoying for exception capture. We need to figure out a way around this,
            # or deprecate this proxy option fully (it's already in the process of deprecation, no new clients should be using this method since like 5-6 months)
            enable_exception_autocapture=enable_exception_autocapture,
            log_captured_exceptions=log_captured_exceptions,
            before_send=before_send,
            enable_local_evaluation=enable_local_evaluation,
            flag_definition_cache_provider=flag_definition_cache_provider,
            capture_exception_code_variables=capture_exception_code_variables,
            code_variables_mask_patterns=code_variables_mask_patterns,
            code_variables_ignore_patterns=code_variables_ignore_patterns,
            in_app_modules=in_app_modules,
        )

    # Always set in case user changes it. Preserve Client's auto-disabled state
    # for API keys that become empty after trimming.
    default_client.disabled = disabled or not default_client.api_key
    default_client.debug = debug
    default_client._set_before_send(before_send)

    return default_client

