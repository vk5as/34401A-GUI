# Error Queue Is Drained After Setup Changes, Not After Every Reading

`SYST:ERR?` is queried until empty after each command that changes the Setup. During Continuous, Single and Burst reading, the application checks `*STB?` bit 2 (error queue not empty) every few seconds, and drains the queue only when that bit is set. Checking after every Reading would add a full round trip per Reading, roughly 30 ms each at 9600 baud over RS-232, and halve throughput at fast Integration Times.
