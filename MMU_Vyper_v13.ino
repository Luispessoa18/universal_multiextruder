
#include <AFMotor.h>
#include <math.h>

AF_Stepper motor1(200, 1);
AF_Stepper motor2(200, 2);

int selecionado = 1;
int rpm = 20;

// Valores provisórios, calibrar individualmente.
float passosMM[2] = {6.0, 6.0};
bool invertido[2] = {true, true};

void mover(long passos) {
  int i = selecionado - 1;

  bool frente = passos > 0;

  if (invertido[i]) {
    frente = !frente;
  }

  uint8_t dir = frente ? FORWARD : BACKWARD;

  long restante = labs(passos);

  AF_Stepper &motor =
    selecionado == 1 ? motor1 : motor2;

  while (restante > 0) {
    int bloco = min(restante, 20L);
    motor.step(bloco, dir, DOUBLE);
    restante -= bloco;
  }

  motor.release();

  Serial.println("ok");
}

void setup() {
  Serial.begin(9600);
  Serial.setTimeout(200);

  motor1.setSpeed(rpm);
  motor2.setSpeed(rpm);

  Serial.println("MMU DUAL BOWDEN");
  Serial.println("T1 / T2 - Selecionar");
  Serial.println("N1 - Uma volta");
  Serial.println("F10 / R10 - Milimetros");
  Serial.println("CAL=6.0 - Passos/mm");
  Serial.println("INV - Inverter sentido");
  Serial.println("RPM=20 - Velocidade");
  Serial.println("ok");
}

void loop() {
  if (!Serial.available()) return;

  String cmd = Serial.readStringUntil('\n');
  cmd.trim();
  cmd.toUpperCase();
  cmd.replace(',', '.');

  if (cmd.length() == 0) return;

  if (cmd == "T1" || cmd == "T2") {
    selecionado = cmd.substring(1).toInt();
    Serial.println("ok");
  }

  else if (cmd == "PING") {
    Serial.println("ok");
  }

  else if (cmd == "INV") {
    invertido[selecionado - 1] =
      !invertido[selecionado - 1];

    Serial.println("ok");
  }

  else if (cmd.startsWith("CAL=")) {
    float v = cmd.substring(4).toFloat();

    if (v <= 0 || v > 500) {
      Serial.println("error: CAL");
      return;
    }

    passosMM[selecionado - 1] = v;
    Serial.println("ok");
  }

  else if (cmd.startsWith("RPM=")) {
    int v = cmd.substring(4).toInt();

    if (v < 1 || v > 60) {
      Serial.println("error: RPM");
      return;
    }

    rpm = v;

    motor1.setSpeed(rpm);
    motor2.setSpeed(rpm);

    Serial.println("ok");
  }

  else if (cmd.startsWith("N")) {
    float voltas = cmd.substring(1).toFloat();

    if (fabs(voltas) > 20) {
      Serial.println("error: LIMIT");
      return;
    }

    mover(lround(voltas * 200.0));
  }

  else if (cmd.startsWith("F") ||
           cmd.startsWith("R")) {

    float mm = cmd.substring(1).toFloat();

    if (mm <= 0 || mm > 300) {
      Serial.println("error: DISTANCE");
      return;
    }

    long passos = lround(
      mm * passosMM[selecionado - 1]
    );

    if (cmd.startsWith("R")) {
      passos = -passos;
    }

    mover(passos);
  }

  else if (cmd == "S") {
    Serial.print("Motor: ");
    Serial.println(selecionado);

    Serial.print("T1 passos/mm: ");
    Serial.println(passosMM[0], 4);

    Serial.print("T2 passos/mm: ");
    Serial.println(passosMM[1], 4);

    Serial.print("RPM: ");
    Serial.println(rpm);

    Serial.println("ok");
  }

  else {
    Serial.println("error: UNKNOWN");
  }
}
