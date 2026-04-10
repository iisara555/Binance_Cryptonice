@{
    RuntimeServiceName = 'CryptoBotRuntime'
    HealthTaskName = 'CryptoBotHealthMonitor'
    BotHealthUrl = 'http://127.0.0.1:8080/health'
    FailureThreshold = 3
    RestartCooldownSeconds = 120
    HealthCheckEveryMinutes = 1
    RuntimeStartTimeoutSeconds = 60
    AllowAuthDegraded = $false
}