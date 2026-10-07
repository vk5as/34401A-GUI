# Read Back the Meter's Setup on Connect; Never Reset Implicitly

On connect, the application queries the Meter's full Setup and fills the UI from it without changing anything. Disconnecting or closing returns the Meter to Local and leaves its Setup as it was. `*RST` happens only when the user presses Reset. A bench Meter is often mid-task, and reconnecting must not silently change what it is measuring. This costs extra code to parse every Setup query, which is accepted.
