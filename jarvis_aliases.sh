# Jarvis Voice Assistant Aliases
alias startjarvis='~/jarvis/start.sh'
alias stopjarvis='~/jarvis/stop.sh'
alias restartjarvis='~/jarvis/restart.sh'
alias jarvisstatus='~/jarvis/status.sh'
alias jarvislogs='journalctl --user -u jarvis.service -f'
alias killjarvis='~/jarvis/killswitch.sh'

# For testing/development (runs in foreground)
alias runjarvis='cd ~/jarvis && python3 jarvis_continuous.py'
