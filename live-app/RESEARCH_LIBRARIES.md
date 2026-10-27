# Optional options research environment

This environment is separate from the production web app and collector. It does not connect broker credentials, change collection, enable trading or provide live signals by itself.

Python 3.12: install the pinned requirements-research.lock.txt in an isolated virtual environment. Dhan's SDK is already in requirements.lock.txt. OpenAlgo is an API client and requires a separately configured OpenAlgo server; installing the client does not install that server or its analytics UI. Smart Money indicators are heuristic and need time-aware validation. XGBoost is the CPU package and no trained model is included. vollib pricing requires validated inputs, chosen model and clearly labeled units. Plotly, pandas, NumPy, SciPy and scikit-learn provide research and visualization building blocks.

Import and calculation smoke check (no network, credentials or trades):

    python -c "import numpy,pandas,scipy,sklearn,plotly,xgboost,openalgo; from smartmoneyconcepts import smc; from vollib.black_scholes import black_scholes; assert abs(black_scholes('c',100,100,1,0.05,0.2)-10.4506)<0.001"

This does not validate data quality, package integration, signal quality, backtesting, or production deployment.
