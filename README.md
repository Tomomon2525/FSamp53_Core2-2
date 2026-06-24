# M5Stack 多チャンネル無線力センサ計測システム

M5Stack Core2 をセンサノードとして使用し、WiFi（UDP）経由でPCにリアルタイムデータを送信・記録する複数デバイス対応の無線計測システムです。

## 主な特徴

- **センサノード**: M5Stack Core2（6台以上同時対応）
- **通信方式**: WiFi UDP（ブロードキャスト＋ユニキャスト）
- **計測チャンネル**: 3ch アナログ電圧（オフセット補正後）+ 6軸IMU（加速度・ジャイロ）+ 3軸力
- **サンプリングレート**: 1〜1000 Hz（デフォルト200 Hz ,6台計測時最大300hz）
- **データ保存形式**: CSV（デバイスごとに個別ファイル）
- **PCクライアント**: Python（matplotlib GUI）
- **ファームウェアビルド**: PlatformIO

## ファイル構成

```
M5Stack-Core2_MEMSmeasurement/
├── FSamp53_Core2-2/          # ファームウェア (PlatformIO)
│   ├── platformio.ini
│   ├── config.txt            # デバイス設定 (SDカード用)
│   ├── config_example.txt    # 設定ファイルの記入例
│   └── src/
│       ├── minimal_main_sensor.cpp   # 本番用（実センサ）
│       └── minimal_main_dammy.cpp    # テスト用（ダミーデータ）
├── pc_client/                # PC側GUIクライアント
│   ├── config.ini            # PC側設定ファイル
│   └── gui_client.py         # GUIクライアント
├── figures/                  # マニュアル用画像
├── system_manual.pdf         # システム説明書
└── README.md
```

## 使い方

詳細は `system_manual.pdf` を参照してください。
