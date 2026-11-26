"""
APEX 受控運算（Universal Computing Adapter 的第一個例子）：sim.pvwatts＝PySAM pvwattsv8 太陽光電模擬。
規則（ADR-10 不變）：節點只收「參數」，不收程式碼。模擬程式寫死在這裡、跟 EXE 一起簽章。
  ・power.py   ：節點主程式抓 NASA POWER 逐時氣象（只准連 power.larc.nasa.gov、存快取）
  ・pvwatts.py ：在 Job Object 沙箱裡用「節點自己設定的 PySAM Python」跑固定的模擬腳本（子程序不能連網）
PySAM 不放進 EXE（光壓縮檔 33 MB，EXE 才 37 MB；朋友只下載影片不需要它）。
"""
