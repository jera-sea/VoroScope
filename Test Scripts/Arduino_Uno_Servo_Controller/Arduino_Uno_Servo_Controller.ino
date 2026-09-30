/* Serial Servo Control with RPM Speed Limit
 
 1. Set the 'SPEED_RPM' variable below to change speed.
 2. Open Serial Monitor (9600 baud).
 3. Enter an angle (0-180).
*/

#include <Servo.h>

Servo myservo; 

// --- USER SETTINGS ---
float SPEED_RPM = 10.0;  // Set the speed in Revolutions Per Minute
// ---------------------

int currentPos = 0;      // Tracks the current angle of the servo
int delayPerDegree;      // Calculated delay in milliseconds

void setup() {
  myservo.attach(11);
  Serial.begin(9600);
  
  // Calculate delay: (1000 ms / (6 * RPM)) = ms per degree
  // Because 1 RPM = 360 degrees per 60 seconds (6 degrees per second)
  delayPerDegree = 1000 / (6 * SPEED_RPM);
  
  // Initialize servo to start position
  myservo.write(currentPos);
  
  Serial.print("Servo Ready at ");
  Serial.print(SPEED_RPM);
  Serial.println(" RPM.");
  Serial.println("Enter target angle (0-180):");
}

void loop() {
  if (Serial.available() > 0) {
    if (isDigit(Serial.peek())) {
      
      int targetPos = Serial.parseInt();
      
      // Basic validation
      if (targetPos >= 0 && targetPos <= 360) {
        
        Serial.print("Moving to ");
        Serial.print(targetPos);
        Serial.println(" deg...");

        // Move the servo smoothly to the target
        moveServoSmoothly(targetPos);
        
        Serial.println("Done.");
      } 
    }
    // Clear buffer
    while (Serial.available() > 0 && Serial.peek() < '0') {
      Serial.read();
    }
  }
}

// Function to handle the slow movement logic
void moveServoSmoothly(int target) {
  
  // If we need to move UP (e.g., from 0 to 90)
  if (target > currentPos) {
    for (int pos = currentPos; pos <= target; pos++) {
      myservo.write(pos);
      delay(delayPerDegree);
    }
  } 
  // If we need to move DOWN (e.g., from 90 to 0)
  else {
    for (int pos = currentPos; pos >= target; pos--) {
      myservo.write(pos);
      delay(delayPerDegree);
    }
  }
  
  // Update the tracker variable
  currentPos = target;
}
