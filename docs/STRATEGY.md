# 📈 Nexus Seeker: Quantitative Strategy & Risk Mathematics

This document defines the mathematical foundations of the **Nexus Risk Optimizer (NRO)**, the **VIX Battle Ladder**, and the **Gamma Fragility Assessment** protocols. 

---

## 1. Portfolio Risk Aggregation (Beta-Weighting)

To maintain a unified view of systemic risk, all positions are normalized to **SPY-equivalent units** using historical correlation (Beta).

### 1.1 Systemic Delta Exposure ($\Delta_{\beta}$)
The portfolio's net directional bias relative to the S&P 500:
$$\Delta_{\beta} = \sum_{i=1}^{n} \left( \delta_i \times Q_i \times 100 \times \beta_i \times \frac{P_i}{P_{SPY}} \right)$$
*   $\delta_i$: Local contract Delta.
*   $Q_i$: Quantity (Negative for STO).
*   $\beta_i$: 60-day historical Beta relative to SPY.
*   $P_i / P_{SPY}$: Price ratio for dollar-neutral normalization.

### 1.2 Gamma Fragility Assessment ($\Gamma_{\beta}$)
Gamma measures the acceleration of Delta. Negative Gamma ($Net\ \Gamma < 0$) indicates **convexity risk**, where losses accelerate as the market moves against the position.
$$\Gamma_{\beta} = \sum_{i=1}^{n} \left( \gamma_i \times Q_i \times 100 \times \beta_i^2 \times \left(\frac{P_i}{P_{SPY}}\right)^2 \right)$$
*   **Threshold:** If $\Gamma_{\beta} < -20.0$, the terminal declares a **Fragile State**, triggering a priority alert to inject positive Gamma or reduce margin heat.

---

## 2. VIX Battle Ladder: Adaptive Scaling Logic

The terminal utilizes a 6-stage response matrix to dynamically adjust risk appetite based on implied volatility (VIX).

### 2.1 Environmental Risk Modifiers
The effective risk limit is computed as:
$$Risk_{adj} = Risk_{base} \times w_{vix} \times w_{oil} \times w_{regime}$$

| VIX Tier | $w_{vix}$ (Weight) | STO Delta Cap | Behavior Mode |
|---|---|---|---|
| **Dormant** (< 15) | 0.0 | N/A | Total Signal Rejection |
| **Caution** (15-18) | 0.5 | -0.12 | Conservative Sizing |
| **Ready** (18-24) | 1.0 | -0.20 | Standard Operations |
| **Aggressive** (24-30) | 1.2 | -0.20 | Tactical Expansion |
| **Heavy** (30-35) | 1.5 | -0.25 | Offensive Posture |
| **Extreme** (≥ 35) | 2.0 | -0.35 | All-in (Bypass Modifiers) |

### 2.2 Dynamic Kelly Criterion Scaling
The terminal utilizes a fractional Kelly Criterion to optimize position sizing while avoiding ruin.
$$f^* = \text{Fraction} \times \frac{p \cdot b - q}{b}$$
*   **Standard Mode:** Uses **1/4 Kelly** (Fraction = 0.25).
*   **High Volatility Insertion:** When $VIX > 29.5$, the terminal linearly interpolates between **1/4 Kelly** and **1/2 Kelly**, reaching max intensity at $VIX = 45$.

---

## 3. Decision Logic & Pipeline Thresholds

### 3.1 Capital Efficiency (AROC)
Minimum yield required to justify margin utilization:
$$AROC = \frac{\text{Premium}}{\text{Margin}} \times \frac{365}{DTE} \times 100$$
*   **STO Requirement:** $\ge 15\%$
*   **BTO Requirement:** $\ge 30\%$ (Expected Move vs. Premium)

### 3.2 DITM Convexity Guard (Profit Lock)
The **Profit Lock** mechanism monitors the loss of convexity in Long Options (BTO). When an option enters Deep-In-The-Money (DITM) territory, it effectively becomes a "Synthetic Underlying" but retains extrinsic decay.
*   **Trigger:** $\Delta \ge 0.85$ **AND** $PnL > 150\%$ **AND** $DTE \le 21$.
*   **Logic:** At $\Delta = 0.85$, the position has minimal remaining leverage (Gamma) relative to capital at risk. The terminal mandates a **Convexity Reset** (Roll or Close).

---

## 4. Financial Runway Analytics

Survival assessment for professional traders:
$$Runway\ (Days) = \frac{\text{Cash Reserve}}{\text{Monthly Expenses} - (\text{Daily Portfolio Theta} \times 30)} \times 30$$
*   **Daily Theta ($\Theta$):** The total dollar amount of time value harvested by the portfolio every 24 hours.
*   **Net Burn Rate:** Monthly expenses offset by Theta cash flow. If $\Theta_{monthly} > Expenses$, $Runway = \infty$.
