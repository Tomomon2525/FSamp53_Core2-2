# M5Stack 多チャンネル無線力センサ計測システムver1.0

M5Stack Core2 をセンサノードとして使用し、WiFi（UDP）経由でPCにリアルタイムデータを送信・記録する複数デバイス対応の無線計測システムです。

## 主な特徴

- **センサノード**: M5Stack Core2（6台以上同時対応）
- **通信方式**: WiFi UDP（ブロードキャスト＋ユニキャスト）
- **計測チャンネル**: 3ch アナログ電圧（オフセット補正後）+ 6軸IMU（加速度・ジャイロ）+ 3軸力
- **サンプリングレート**: 1〜1000 Hz（デフォルト200 Hz ,6台同時計測の場合300Hz）
- **データ保存形式**: CSV（デバイスごとに個別ファイル）
- **PCクライアント**: Python（matplotlib GUI）
- **ファームウェアビルド**: PlatformIO

## ファイル構成

```
M5Stack-Core2-2/
├── FSamp53_Core2-2/          # ファームウェア (PlatformIO)
│   ├── platformio.ini
│   ├── config.txt            # デバイス設定 (SDカード用)
│   ├── config_example.txt    # 設定ファイルの記入例
│   └── src/
│       ├── M5_main_sensor_ver1.0.cpp   # 本番用（実センサ）
│       └── M5_main_dammy_ver1.0.cpp    # テスト用（ダミーデータ）
├── pc_client/                # PC側GUIクライアント
│   ├── config.ini            # PC側設定ファイル
│   └── gui_client_ver1.0.py         # GUIクライアント
├── figures/                  # マニュアル用画像
├── system_manual_ver1.0.pdf         # システム説明書
└── README.md
```

## 使い方

詳細は `system_manual.pdf` を参照してください。
