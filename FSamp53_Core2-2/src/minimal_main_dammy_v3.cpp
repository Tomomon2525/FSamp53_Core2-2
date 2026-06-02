#include <Arduino.h>
#include "M5Unified.h"
#include <WiFi.h>
#include <WiFiUdp.h>
#include <SD.h>
#include <SPI.h>

// ===== Configuration (loaded from SD card /config.txt) =====
char cfgWifiSSID[64]     = "";
char cfgWifiPassword[64] = "";
char cfgDeviceName[32]   = "";
uint8_t  cfgDeviceID     = 0;
uint16_t cfgDataPort     = 0;
uint16_t cfgCmdPort      = 0;
uint32_t cfgSampleRate   = 0;

#define PIN_INA 35
#define PIN_INB 36
#define PIN_INC 34
#define COLOR_A TFT_RED
#define COLOR_B TFT_GREEN
#define COLOR_C TFT_YELLOW

const uint16_t HEADER_MARKER = 0xAAAA;
const uint16_t FOOTER_MARKER = 0x5555;
const int      PACKET_SIZE   = 37;

// MATRIX (loaded from config.txt)
float MATRIX[3][3] = {{0, 0, 0},
                      {0, 0, 0},
                      {0, 0, 0}};

WiFiUDP udpData;
WiFiUDP udpCmd;

uint8_t packetBuf[PACKET_SIZE];
volatile bool isMeasuring = false;
volatile int displayMode = 0;       // 0:Raw Volt, 1:Off Volt, 2:IMU, 3:Force
volatile bool displayModeLocked = false;
float offsetA = 0, offsetB = 0, offsetC = 0;

volatile uint64_t baseUnixTime = 0;
volatile uint32_t baseMillis = 0;
volatile int pendingPackets = 0;
volatile uint32_t packetCount = 0;
volatile uint32_t currentRateHz = 200;
hw_timer_t *timer = NULL;
IPAddress pcIP;

volatile int currentY[6] = {0, 0, 0, 0, 0, 0};
int displayWidth, displayHeight;
const int LEFT_MARGIN = 25;

// Button B long-press handling
bool btnBLongPressHandled = false;

// ===== Parse a MATRIX row from comma-separated string =====
// e.g. "0.01178285, 0.42417336, -0.62513834"
void parseMatrixRow(int row, const String &val) {
    int idx = 0;
    int start = 0;
    for (int col = 0; col < 3; col++) {
        int comma = val.indexOf(',', start);
        String token;
        if (comma < 0) {
            token = val.substring(start);
        } else {
            token = val.substring(start, comma);
            start = comma + 1;
        }
        token.trim();
        MATRIX[row][col] = token.toFloat();
    }
}

// ===== Load config from SD card =====
bool loadConfig() {
    // M5Stack Core2: SD card uses VSPI, CS=GPIO4
    if (!SD.begin(4, SPI, 25000000)) {
        Serial.println("[CONFIG] SD card mount failed");
        return false;
    }
    if (!SD.exists("/config.txt")) {
        Serial.println("[CONFIG] /config.txt not found on SD, using defaults");
        return false;
    }
    File f = SD.open("/config.txt", FILE_READ);
    if (!f) {
        Serial.println("[CONFIG] Failed to open /config.txt");
        return false;
    }
    Serial.println("[CONFIG] Loading /config.txt from SD card ...");
    while (f.available()) {
        String line = f.readStringUntil('\n');
        line.trim();
        if (line.length() == 0 || line.startsWith("#")) continue;
        int sep = line.indexOf('=');
        if (sep < 0) continue;
        String key = line.substring(0, sep);
        String val = line.substring(sep + 1);
        key.trim(); val.trim();
        if (key == "wifi_ssid")        { strncpy(cfgWifiSSID, val.c_str(), sizeof(cfgWifiSSID) - 1); }
        else if (key == "wifi_password") { strncpy(cfgWifiPassword, val.c_str(), sizeof(cfgWifiPassword) - 1); }
        else if (key == "device_id")    { cfgDeviceID = (uint8_t)val.toInt(); }
        else if (key == "device_name")  { strncpy(cfgDeviceName, val.c_str(), sizeof(cfgDeviceName) - 1); }
        else if (key == "data_port")    { cfgDataPort = (uint16_t)val.toInt(); }
        else if (key == "cmd_port")     { cfgCmdPort  = (uint16_t)val.toInt(); }
        else if (key == "sample_rate")  { cfgSampleRate = (uint32_t)val.toInt(); }
        else if (key == "matrix_row0")  { parseMatrixRow(0, val); }
        else if (key == "matrix_row1")  { parseMatrixRow(1, val); }
        else if (key == "matrix_row2")  { parseMatrixRow(2, val); }
        Serial.printf("[CONFIG]  %s = %s\n", key.c_str(), val.c_str());
    }
    f.close();
    Serial.println("[CONFIG] Done.");
    Serial.println("[CONFIG] MATRIX:");
    for (int r = 0; r < 3; r++) {
        Serial.printf("[CONFIG]   [%+.8f, %+.8f, %+.8f]\n", MATRIX[r][0], MATRIX[r][1], MATRIX[r][2]);
    }
    return true;
}

// ===== Dummy data generator (replaces real ADC) =====
float getDummyMilliVolts(int pin) {
    float timeSec = millis() / 1000.0f;
    float baseVolts = 1650.0f;
    float amplitude = 500.0f;
    float freq = 1.0f;
    if (pin == PIN_INB) freq = 2.0f;
    if (pin == PIN_INC) freq = 0.5f;
    float noise = random(-50, 50);
    return baseVolts + amplitude * sin(2 * PI * freq * timeSec) + noise;
}

// ===== Display Functions (from main.cpp) =====
void drawModeLabel() {
    // Header area: 2 rows (0-30)
    M5.Display.fillRect(0, 0, displayWidth, 30, TFT_BLACK);

    // Row 1: Device Name & ID
    M5.Display.setTextColor(TFT_CYAN, TFT_BLACK);
    M5.Display.setTextSize(1);
    M5.Display.setCursor(10, 2);
    M5.Display.printf("%s (ID:%d)", cfgDeviceName, cfgDeviceID);

    // Battery level (top-right)
    int batteryPercent = M5.Power.getBatteryLevel();
    M5.Display.setTextColor(TFT_WHITE, TFT_BLACK);
    M5.Display.setCursor(displayWidth - 42, 2);
    M5.Display.printf("%d%%", batteryPercent);

    // Row 2: Mode / Lock / Legend
    M5.Display.setTextColor(TFT_WHITE, TFT_BLACK);
    M5.Display.setCursor(10, 18);
    const char *modeStr[] = {"Raw Volt", "Off Volt", "IMU", "Force"};
    M5.Display.printf("Mode: %s", modeStr[displayMode]);

    // Lock indicator
    if (displayModeLocked) {
        M5.Display.setCursor(displayWidth / 2 - 15, 18);
        M5.Display.printf("LOCKED");
    }

    // Legend
    int startX = displayWidth - 130;
    int y = 23;
    int radius = 4;

    const char* labelA = (displayMode == 2 || displayMode == 3) ? "X" : "A";
    const char* labelB = (displayMode == 2 || displayMode == 3) ? "Y" : "B";
    const char* labelC = (displayMode == 2 || displayMode == 3) ? "Z" : "C";

    M5.Display.fillCircle(startX, y, radius, COLOR_A);
    M5.Display.setCursor(startX + 10, 18);
    M5.Display.printf("%s", labelA);

    startX += 30;
    M5.Display.fillCircle(startX, y, radius, COLOR_B);
    M5.Display.setCursor(startX + 10, 18);
    M5.Display.printf("%s", labelB);

    startX += 30;
    M5.Display.fillCircle(startX, y, radius, COLOR_C);
    M5.Display.setCursor(startX + 10, 18);
    M5.Display.printf("%s", labelC);
}

void drawLeftScale() {
    M5.Display.fillRect(0, 30, LEFT_MARGIN, displayHeight - 30, TFT_BLACK);
    M5.Display.setTextColor(TFT_WHITE, TFT_BLACK);
    M5.Display.setTextSize(1);

    if (displayMode == 0) {
        float voltages[] = {0, 0.8, 1.65, 2.5, 3.3};
        for (int i = 0; i < 5; i++) {
            int y = map((int)(voltages[i] * 1000), 0, 3300, displayHeight - 1, 20);
            M5.Display.drawLine(LEFT_MARGIN - 3, y, LEFT_MARGIN - 1, y, TFT_WHITE);
            M5.Display.setCursor(1, y - 3);
            M5.Display.printf("%.1f", voltages[i]);
        }
        M5.Display.setCursor(2, 22); M5.Display.printf("V");

    } else if (displayMode == 1) {
        float voltages[] = {-2.0, -1.0, 0.0, 1.0, 2.0};
        for (int i = 0; i < 5; i++) {
            int y = map((int)(voltages[i] * 1000), -2000, 2000, displayHeight - 1, 20);
            M5.Display.drawLine(LEFT_MARGIN - 3, y, LEFT_MARGIN - 1, y, TFT_WHITE);
            M5.Display.setCursor(1, y - 3);
            M5.Display.printf("%+.1f", voltages[i]);
        }
        M5.Display.setCursor(2, 22); M5.Display.printf("V");

    } else if (displayMode == 2) {
        int midY = (displayHeight + 20) / 2;
        float accelMarks[] = {-1.0, -0.5, 0.0, 0.5, 1.0};
        for (int i = 0; i < 5; i++) {
            int y = constrain(map((long)(accelMarks[i] * 1000), -1000, 1000, midY - 1, 20), 20, midY - 1);
            M5.Display.drawLine(LEFT_MARGIN - 3, y, LEFT_MARGIN - 1, y, TFT_WHITE);
            M5.Display.setCursor(1, y - 3);
            M5.Display.printf("%+.1f", accelMarks[i]);
        }
        M5.Display.setCursor(2, 22); M5.Display.print("m");
        M5.Display.setCursor(2, 30); M5.Display.print("s2");

        float gyroMarks[] = {-250, -125, 0, 125, 250};
        for (int i = 0; i < 5; i++) {
            int y = constrain(map((long)gyroMarks[i], -250, 250, displayHeight - 1, midY), midY, displayHeight - 1);
            M5.Display.drawLine(LEFT_MARGIN - 3, y, LEFT_MARGIN - 1, y, TFT_WHITE);
            M5.Display.setCursor(1, y - 3);
            M5.Display.printf("%+d", (int)gyroMarks[i]);
        }
        M5.Display.setCursor(2, midY + 2); M5.Display.print("d");
        M5.Display.setCursor(2, midY + 10); M5.Display.print("ps");

    } else if (displayMode == 3) {
        float forceMarks[] = {-2.0, -1.0, 0.0, 1.0, 2.0};
        for (int i = 0; i < 5; i++) {
            int y = map((int)(forceMarks[i] * 1000), -2000, 2000, displayHeight - 1, 20);
            M5.Display.drawLine(LEFT_MARGIN - 3, y, LEFT_MARGIN - 1, y, TFT_WHITE);
            M5.Display.setCursor(1, y - 3);
            M5.Display.printf("%+.1f", forceMarks[i]);
        }
        M5.Display.setCursor(2, 22); M5.Display.printf("N");
    }
}

void drawIMUSeparator() {
    if (displayMode == 2) {
        int midY = (displayHeight + 20) / 2;
        M5.Display.drawLine(LEFT_MARGIN, midY, displayWidth - 1, midY, TFT_WHITE);
    }
}

void refreshScreen() {
    M5.Display.fillScreen(TFT_BLACK);
    drawModeLabel();
    drawLeftScale();
    drawIMUSeparator();
}

void startOffsetCollection() {
    long sA = 0, sB = 0, sC = 0;
    for (int i = 0; i < 50; i++) {
        sA += getDummyMilliVolts(PIN_INA);
        sB += getDummyMilliVolts(PIN_INB);
        sC += getDummyMilliVolts(PIN_INC);
        delay(2);
    }
    offsetA = sA / 50.0f;
    offsetB = sB / 50.0f;
    offsetC = sC / 50.0f;
}

// ===== Timer ISR =====
void IRAM_ATTR onTimer() {
    if (isMeasuring) pendingPackets++;
}

// ===== UDP send task (Core 0) =====
void udpTask(void *pvParameters) {
    while (1) {
        if (isMeasuring && pendingPackets > 0) {
            pendingPackets--;
            M5.Imu.update();
            float ax, ay, az, gx, gy, gz;
            M5.Imu.getAccel(&ax, &ay, &az);
            M5.Imu.getGyro(&gx, &gy, &gz);
            float rA = getDummyMilliVolts(PIN_INA), rB = getDummyMilliVolts(PIN_INB), rC = getDummyMilliVolts(PIN_INC);
            float vA = (rA - offsetA) / 1000.0f, vB = (rB - offsetB) / 1000.0f, vC = (rC - offsetC) / 1000.0f;
            float fx = MATRIX[0][0]*vA + MATRIX[0][1]*vB + MATRIX[0][2]*vC;
            float fy = MATRIX[1][0]*vA + MATRIX[1][1]*vB + MATRIX[1][2]*vC;
            float fz = MATRIX[2][0]*vA + MATRIX[2][1]*vB + MATRIX[2][2]*vC;

            int o = 0;
            packetBuf[o++] = 0xAA; packetBuf[o++] = 0xAA; packetBuf[o++] = cfgDeviceID;
            uint64_t ts = baseUnixTime + (packetCount * 1000ULL / currentRateHz);
            packetCount++;
            for (int i = 7; i >= 0; i--) packetBuf[o++] = (ts >> (i * 8)) & 0xFF;
            int16_t ch[12] = {(int16_t)(vA*1000), (int16_t)(vB*1000), (int16_t)(vC*1000),
                              (int16_t)(ax*1000), (int16_t)(ay*1000), (int16_t)(az*1000),
                              (int16_t)(gx*10),   (int16_t)(gy*10),   (int16_t)(gz*10),
                              (int16_t)(fx*1000), (int16_t)(fy*1000), (int16_t)(fz*1000)};
            for (int i = 0; i < 12; i++) { packetBuf[o++] = (ch[i] >> 8) & 0xFF; packetBuf[o++] = ch[i] & 0xFF; }
            packetBuf[o++] = 0x55; packetBuf[o++] = 0x55;
            udpData.beginPacket(pcIP, cfgDataPort);
            udpData.write(packetBuf, PACKET_SIZE);
            udpData.endPacket();

            // Update graph data for non-measuring display (currentY) — used when STOP resumes graph
            if (displayMode == 0) { currentY[0]=map((long)rA,0,3300,239,20); currentY[1]=map((long)rB,0,3300,239,20); currentY[2]=map((long)rC,0,3300,239,20); }
            else if (displayMode == 1) { currentY[0]=map((long)(vA*1000),-2000,2000,239,20); currentY[1]=map((long)(vB*1000),-2000,2000,239,20); currentY[2]=map((long)(vC*1000),-2000,2000,239,20); }
            else if (displayMode == 2) { int mid=(239+20)/2; currentY[0]=map((long)(ax*1000),-1000,1000,mid-1,20); currentY[1]=map((long)(ay*1000),-1000,1000,mid-1,20); currentY[2]=map((long)(az*1000),-1000,1000,mid-1,20); currentY[3]=map((long)gx,-250,250,239,mid); currentY[4]=map((long)gy,-250,250,239,mid); currentY[5]=map((long)gz,-250,250,239,mid); }
            else { currentY[0]=map((long)(fx*1000),-2000,2000,239,20); currentY[1]=map((long)(fy*1000),-2000,2000,239,20); currentY[2]=map((long)(fz*1000),-2000,2000,239,20); }
        } else if (!isMeasuring) {
            M5.Imu.update();
            float ax, ay, az, gx, gy, gz;
            M5.Imu.getAccel(&ax, &ay, &az);
            M5.Imu.getGyro(&gx, &gy, &gz);
            float rA = getDummyMilliVolts(PIN_INA), rB = getDummyMilliVolts(PIN_INB), rC = getDummyMilliVolts(PIN_INC);
            float vA = (rA - offsetA) / 1000.0f, vB = (rB - offsetB) / 1000.0f, vC = (rC - offsetC) / 1000.0f;
            if (displayMode == 0) { currentY[0]=map((long)rA,0,3300,239,20); currentY[1]=map((long)rB,0,3300,239,20); currentY[2]=map((long)rC,0,3300,239,20); }
            else if (displayMode == 1) { currentY[0]=map((long)(vA*1000),-2000,2000,239,20); currentY[1]=map((long)(vB*1000),-2000,2000,239,20); currentY[2]=map((long)(vC*1000),-2000,2000,239,20); }
            else if (displayMode == 2) { int mid=(239+20)/2; currentY[0]=map((long)(ax*1000),-1000,1000,mid-1,20); currentY[1]=map((long)(ay*1000),-1000,1000,mid-1,20); currentY[2]=map((long)(az*1000),-1000,1000,mid-1,20); currentY[3]=map((long)gx,-250,250,239,mid); currentY[4]=map((long)gy,-250,250,239,mid); currentY[5]=map((long)gz,-250,250,239,mid); }
            else { float fx=MATRIX[0][0]*vA+MATRIX[0][1]*vB+MATRIX[0][2]*vC, fy=MATRIX[1][0]*vA+MATRIX[1][1]*vB+MATRIX[1][2]*vC, fz=MATRIX[2][0]*vA+MATRIX[2][1]*vB+MATRIX[2][2]*vC; currentY[0]=map((long)(fx*1000),-2000,2000,239,20); currentY[1]=map((long)(fy*1000),-2000,2000,239,20); currentY[2]=map((long)(fz*1000),-2000,2000,239,20); }
            vTaskDelay(10);
        } else {
            vTaskDelay(1);
        }
        for (int i = 0; i < 6; i++) currentY[i] = constrain(currentY[i], 20, 239);
    }
}

// ===== Setup =====
void setup() {
    M5.begin();
    M5.Imu.begin();
    Serial.begin(115200);

    // Load config from SD card
    loadConfig();
    currentRateHz = cfgSampleRate;

    displayWidth = M5.Display.width();
    displayHeight = M5.Display.height();

    M5.Display.fillScreen(TFT_BLACK);
    M5.Display.setTextSize(1);

    // ----- WiFi connection with GUI status -----
    M5.Display.setTextColor(TFT_CYAN, TFT_BLACK);
    M5.Display.setCursor(10, 30);
    M5.Display.printf("Connecting to %s...", cfgWifiSSID);

    WiFi.mode(WIFI_STA);
    WiFi.begin(cfgWifiSSID, cfgWifiPassword);

    int wifiTimeout = 0;
    while (WiFi.status() != WL_CONNECTED && wifiTimeout < 20) {
        delay(500);
        M5.Display.print(".");
        wifiTimeout++;
    }

    if (WiFi.status() == WL_CONNECTED) {
        M5.Display.setCursor(10, 50);
        M5.Display.setTextColor(TFT_GREEN, TFT_BLACK);
        M5.Display.printf("Connected! IP: %s", WiFi.localIP().toString().c_str());

        M5.Display.setCursor(10, 70);
        M5.Display.setTextColor(TFT_WHITE, TFT_BLACK);
        M5.Display.printf("MAC: %s", WiFi.macAddress().c_str());
    } else {
        M5.Display.setCursor(10, 50);
        M5.Display.setTextColor(TFT_RED, TFT_BLACK);
        M5.Display.printf("WiFi FAILED!");
    }

    M5.Display.setCursor(10, 100);
    M5.Display.setTextColor(TFT_YELLOW, TFT_BLACK);
    M5.Display.printf("Dev:%s (ID:%d)", cfgDeviceName, cfgDeviceID);
    M5.Display.setCursor(10, 120);
    M5.Display.printf("CMD Port:%d  Rate:%dHz", cfgCmdPort, currentRateHz);
    M5.Display.setCursor(10, 140);
    M5.Display.printf("(Dummy mode)");
    M5.Display.setCursor(10, 160);
    M5.Display.setTextColor(TFT_MAGENTA, TFT_BLACK);
    M5.Display.printf("Waiting for START command...");

    delay(2000);

    // ----- Init -----
    startOffsetCollection();
    udpCmd.begin(cfgCmdPort);
    refreshScreen();

    timer = timerBegin(0, 80, true);
    timerAttachInterrupt(timer, &onTimer, true);
    timerAlarmWrite(timer, 1000000 / currentRateHz, true);
    timerAlarmEnable(timer);

    xTaskCreatePinnedToCore(udpTask, "udpTask", 8192, NULL, 5, NULL, 0);
}

// ===== Main Loop =====
void loop() {
    M5.update();

    // ----- Button A: Re-calibrate offset (unlocked only) -----
    if (M5.BtnA.wasPressed() && !displayModeLocked && !isMeasuring) {
        startOffsetCollection();
        refreshScreen();
    }

    // ----- Button B: Long press 3s = Lock/Unlock -----
    if (M5.BtnB.isPressed()) {
        if (M5.BtnB.pressedFor(3000) && !btnBLongPressHandled) {
            displayModeLocked = !displayModeLocked;
            btnBLongPressHandled = true;
            refreshScreen();
        }
    } else {
        btnBLongPressHandled = false;
    }

    // ----- Button C: Cycle display mode (unlocked & not measuring) -----
    if (M5.BtnC.wasPressed() && !displayModeLocked && !isMeasuring) {
        displayMode = (displayMode + 1) % 4;
        refreshScreen();
    }

    // ----- UDP Command handler -----
    int packetSize = udpCmd.parsePacket();
    if (packetSize) {
        char buf[64];
        int len = udpCmd.read(buf, sizeof(buf) - 1);
        if (len > 0) {
            buf[len] = 0;
            if (strncmp(buf, "START", 5) == 0) {
                if (len > 6) baseUnixTime = strtoull(&buf[6], NULL, 10); else baseUnixTime = 0;
                baseMillis = millis(); pendingPackets = 0; packetCount = 0; isMeasuring = true;
                pcIP = udpCmd.remoteIP();
                // Send device name back to client
                {
                    char nameResp[64];
                    snprintf(nameResp, sizeof(nameResp), "DEVICE_NAME %d %s", cfgDeviceID, cfgDeviceName);
                    udpCmd.beginPacket(udpCmd.remoteIP(), udpCmd.remotePort());
                    udpCmd.write((uint8_t *)nameResp, strlen(nameResp));
                    udpCmd.endPacket();
                }
                // Show MEASURING screen
                M5.Display.fillScreen(TFT_BLACK);
                M5.Display.setTextColor(TFT_GREEN);
                M5.Display.setTextSize(2);
                M5.Display.setCursor(50, 80);
                M5.Display.println("MEASURING...");
                M5.Display.setTextSize(1);
                M5.Display.setCursor(50, 120);
                M5.Display.setTextColor(TFT_WHITE);
                M5.Display.printf("-> %s:%d", pcIP.toString().c_str(), cfgDataPort);
                M5.Display.setCursor(50, 140);
                M5.Display.printf("Rate: %d Hz", currentRateHz);

            } else if (strcmp(buf, "STOP") == 0) {
                isMeasuring = false;
                refreshScreen();

            } else if (strcmp(buf, "SEARCH") == 0) {
                String mac = WiFi.macAddress();
                char resp[128];
                snprintf(resp, sizeof(resp), "SEARCH_ACK %s %d %s", mac.c_str(), cfgDeviceID, cfgDeviceName);
                udpCmd.beginPacket(udpCmd.remoteIP(), udpCmd.remotePort());
                udpCmd.write((uint8_t *)resp, strlen(resp));
                udpCmd.endPacket();

            } else if (strncmp(buf, "RATE", 4) == 0) {
                int rate_hz = atoi(&buf[5]);
                if (rate_hz > 0 && rate_hz <= 1000) {
                    currentRateHz = rate_hz;
                    uint64_t interval_us = 1000000 / rate_hz;
                    timerAlarmDisable(timer);
                    timerAlarmWrite(timer, interval_us, true);
                    timerAlarmEnable(timer);
                }
            }
        }
    }

    // ----- Graph drawing (only when NOT measuring) -----
    static int px = LEFT_MARGIN;
    static int py[6] = {-1, -1, -1, -1, -1, -1};
    if (!isMeasuring) {
        uint16_t clrs[3] = {COLOR_A, COLOR_B, COLOR_C};
        int nl = (displayMode == 2) ? 6 : 3;
        for (int i = 0; i < nl; i++) {
            if (py[i] != -1) M5.Display.drawLine(px - 1, py[i], px, currentY[i], clrs[i % 3]);
            py[i] = currentY[i];
        }
        px++;
        if (px >= displayWidth) {
            px = LEFT_MARGIN;
            refreshScreen();
            for (int i = 0; i < 6; i++) py[i] = -1;
        }
        delay(20);
    } else {
        px = LEFT_MARGIN;
        for (int i = 0; i < 6; i++) py[i] = -1;
        delay(100);
    }
}
