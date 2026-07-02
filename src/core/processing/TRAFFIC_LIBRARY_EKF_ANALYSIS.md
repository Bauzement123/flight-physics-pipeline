# Technical Analysis of the Extended Kalman Filter (EKF) in Python-Traffic

This document provides a comprehensive technical reference for the Extended Kalman Filter (EKF) implementation within the `traffic` library and its integration into our trajectory processing pipeline. It details the underlying mathematical mechanics, expected inputs/outputs, configuration parameters, and specific NaN generation risk factors.

---

## 1. Mathematical & Algorithmic Mechanics

The EKF implementation in the `traffic` library is designed for 6D kinematic smoothing of aircraft trajectories. It estimates the state of the aircraft by recursively linearizing nonlinear kinematic models around the current mean and covariance.

### 1.1 State Vector ($x$)
The filter maintains a **6D state vector** tracking the aircraft's spatial and kinematic variables at time step $k$:
$$x_k = \begin{bmatrix} x_k \\ y_k \\ z^{baro}_k \\ \theta_k \\ v_k \\ w_k \end{bmatrix}$$

Where:
*   $x_k, y_k$: Projected horizontal coordinates (meters, projected using a custom local Lambert Azimuthal Equal Area (LAEA) projection centered at the flight's average coordinates).
*   $z^{baro}_k$: Barometric altitude (meters).
*   $\theta_k$: Heading angle in math radians (unwrapped, counter-clockwise from the East).
*   $v_k$: Groundspeed / horizontal velocity (m/s).
*   $w_k$: Vertical rate / rate of climb and descent (m/s).

### 1.2 Process Model (State Transition)
The state transition function $f(x_{k-1}, \Delta t)$ predicts the state $x_k$ at time step $k$ given the state at $k-1$ and the time delta $\Delta t = t_k - t_{k-1}$:

$$x_k = f(x_{k-1}, \Delta t) + q_{k-1}$$

The mathematical equations for the prediction step are:
$$\begin{aligned}
x_k &= x_{k-1} + v_{k-1} \cos(\theta_{k-1}) \Delta t \\
y_k &= y_{k-1} + v_{k-1} \sin(\theta_{k-1}) \Delta t \\
z^{baro}_k &= z^{baro}_{k-1} + w_{k-1} \Delta t \\
\theta_k &= \theta_{k-1} \\
v_k &= v_{k-1} \\
w_k &= w_{k-1}
\end{aligned}$$

Where heading ($\theta$), velocity ($v$), and vertical rate ($w$) are assumed to be constant over the short time interval $\Delta t$ (zero-order hold), perturbed only by process noise $q_{k-1}$.

### 1.3 Jacobian of the State Transition Matrix ($F$)
Because the state transition function is nonlinear with respect to heading ($\theta$) and velocity ($v$), the covariance propagation requires the Jacobian matrix $F_k = \frac{\partial f}{\partial x}\Big|_{\hat{x}_{k-1}}$:

$$F_k = \begin{bmatrix} 
1 & 0 & 0 & -v \sin(\theta) \Delta t & \cos(\theta) \Delta t & 0 \\
0 & 1 & 0 &  v \cos(\theta) \Delta t & \sin(\theta) \Delta t & 0 \\
0 & 0 & 1 & 0 & 0 & \Delta t \\
0 & 0 & 0 & 1 & 0 & 0 \\
0 & 0 & 0 & 0 & 1 & 0 \\
0 & 0 & 0 & 0 & 0 & 1
\end{bmatrix}$$

This matrix linearizes the nonlinear spatial transitions ($x, y$) with respect to the velocity and orientation of the aircraft.

### 1.4 Covariance Propagation (Prediction)
The predicted covariance matrix $P_k^-$ is calculated using the Jacobian matrix $F_k$ and the process noise covariance matrix $Q_k$:
$$P_k^- = F_k P_{k-1} F_k^T + Q_k$$

### 1.5 Measurement Update Step (Correction)
Since all elements of the state vector are directly observed (via $x, y$, altitude, heading, groundspeed, and vertical rate measurements), the measurement matrix $H$ is a $6 \times 6$ identity matrix:
$$H = I_6$$

The filter updates the state estimate and covariance using the measurement vector $z_k$ and the measurement noise covariance $R_k$:
1.  **Innovation (Measurement Residual):** $y_k = z_k - H x_k^-$
2.  **Innovation Covariance:** $S_k = H P_k^- H^T + R_k = P_k^- + R_k$
3.  **Kalman Gain:** $K_k = P_k^- H^T S_k^{-1} = P_k^- (P_k^- + R_k)^{-1}$
4.  **Updated State Estimate:** $x_k = x_k^- + K_k y_k$
5.  **Updated Covariance Estimate:** $P_k = (I - K_k H) P_k^- = (I - K_k) P_k^-$

### 1.6 Rauch-Tung-Striebel (RTS) Smoothing
If `smooth=True`, a backward pass is executed once the forward Kalman filter finishes. The RTS smoother refines the estimates by propagating information from the end of the flight back to the beginning:

$$\begin{aligned}
C_k &= P_k F_{k+1}^T (P_{k+1}^-)^{-1} \\
\hat{x}_{k|N} &= \hat{x}_k + C_k (\hat{x}_{k+1|N} - \hat{x}_{k+1}^-) \\
P_{k|N} &= P_k + C_k (P_{k+1|N} - P_{k+1}^-) C_k^T
\end{aligned}$$

This results in highly smoothed state vectors by incorporating future trajectory data.

---

## 2. Inputs & Outputs

The EKF operates on a pandas DataFrame containing specific columns representing the flight waypoints.

### 2.1 EKF Inputs (Preprocessed DataFrame)
The EKF expects the input DataFrame to contain the following columns:

| Column Name | Data Type | Units | Description |
| :--- | :--- | :--- | :--- |
| `timestamp` | `datetime64[ns, UTC]` | - | Time index (pandas timezone-aware DatetimeIndex) |
| `x` | `float64` | meters | Projected 2D Cartesian X coordinate (custom LAEA projection) |
| `y` | `float64` | meters | Projected 2D Cartesian Y coordinate (custom LAEA projection) |
| `altitude` | `float64` | feet | Barometric altitude (measured in feet) |
| `track` | `float64` | degrees | Course/heading angle (compass degrees, $0^\circ \text{ to } 360^\circ$) |
| `groundspeed` | `float64` | knots | Horizontal ground speed |
| `vertical_rate` | `float64` | ft/min | Rate of climb/descent |

### 2.2 EKF Outputs (Postprocessed DataFrame)
The EKF function returns the original DataFrame updated with the following smoothed variables:

| Column Name | Data Type | Units | Description |
| :--- | :--- | :--- | :--- |
| `x` | `float64` | meters | Smoothed projected X coordinate |
| `y` | `float64` | meters | Smoothed projected Y coordinate |
| `altitude` | `float64` | feet | Smoothed barometric altitude |
| `track` | `float64` | degrees | Smoothed heading angle ($0^\circ - 360^\circ$, wrapped) |
| `groundspeed` | `float64` | knots | Smoothed ground speed |
| `vertical_rate` | `float64` | ft/min | Smoothed rate of climb/descent |

---

## 3. Configuration Parameters

The filter is configured during instantiation. Its primary parameters control outlier rejection and smoothing.

### 3.1 Class Parameters
The constructor for the EKF class accepts two main parameters:

| Parameter | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `smooth` | `bool` | `True` | If `True`, applies the Rauch-Tung-Striebel (RTS) backward smoothing pass after the forward filter completes. |
| `reject_sigma` | `int` | `3` | Outlier rejection threshold. If a measurement deviates from the predicted state by more than `reject_sigma` standard deviations, the measurement is rejected and replaced with the filter prediction. |

### 3.2 Noise Covariance Matrices ($R$ and $Q$)
The EKF dynamically computes the measurement noise covariance $R_k$ and process noise covariance $Q_k$ using a rolling standard deviation window of the measurements:

*   **Window Size:** $W = 17$ waypoints.
*   **Measurement Noise Covariance ($R$):** Represents the variance of the high-frequency measurement noise. Calculated as a diagonal matrix using the rolling standard deviation of the difference between the raw measurements and their 17-point rolling mean:
    $$R = \text{diag}\left( \sigma_x^2, \sigma_y^2, \sigma_{alt}^2, \sigma_{\theta}^2, \sigma_{speed}^2, \sigma_{vert}^2 \right)$$
    Where:
    $$\sigma_i = \text{std}\left( u_i - \text{rolling\_mean}(u_i, 17) \right)$$
*   **Process Noise Covariance ($Q$):** Represents the modeling errors. Derived by scaling the measurement noise covariance matrix:
    $$Q = \text{diag}\left( 0.1, 0.1, 0.01, 0.3, 1.0, 0.5 \right) \times R$$

---

## 4. NaN Generation Risk Factors

Even when geographic inputs (`latitude`, `longitude`, `time`, `geoaltitude`) are fully valid, the EKF can produce 100% NaN values for `gs`, `heading`, `altitude`, and `rocd`. Below are the primary technical risk factors:

### 4.1 Index Alignment Disconnect (Critical)
*   **Mechanism:** The `preprocess` step of `ProcessXYZZFilterBase` sets the index of the measurement DataFrame to a `DatetimeIndex` (using `df["timestamp"]`):
    ```python
    return pd.DataFrame({ ... }).set_index(df["timestamp"])
    ```
    The Kalman filter and RTS smoother process this DataFrame, returning the states with this `DatetimeIndex`. The `postprocess` method retains this index.
    However, when the EKF assigns these outputs back to the original DataFrame in `EKF.apply`:
    ```python
    return data.assign(**self.postprocess(filtered_states))
    ```
    If `data` (which is `f_projected.data`) uses a **`RangeIndex`** (0, 1, 2...) instead of a matching `DatetimeIndex`, pandas will attempt to align the indexes. Because a `RangeIndex` and a `DatetimeIndex` have no matching keys, pandas fails to align the rows and fills the assigned columns (`altitude`, `track`, `groundspeed`, `vertical_rate`, `x`, and `y`) with **100% NaN** values.
*   **Result:** The trajectory's coordinates (`latitude`, `longitude`, `geoaltitude`) remain valid, but all EKF-smoothed columns are wiped to NaN.

### 4.2 Trajectory Segment Length < Window Size
*   **Mechanism:** The calculation of the diagonal elements of the measurement covariance matrix $R$ utilizes a rolling window of size $W = 17$:
    ```python
    measurements.x.rolling(window_size).mean()
    ```
    If the airborne segment of a flight is shorter than 17 waypoints, the rolling mean cannot be calculated (it returns 100% NaN). Consequently, the standard deviation `.std()` returns `NaN`.
*   **Result:** The entire diagonal of the matrices $R$ and $Q$ becomes `NaN`. This propagates through the Kalman update equations, causing the state vector $x_k$ and covariance $P_k$ to become entirely `NaN` from the first step onward.

### 4.3 Time Series Discontinuities or Singular Covariance
*   **Mechanism:** If any state variable is completely constant during the window (such as a constant cruise altitude or a vertical rate of exactly 0), its standard deviation will be 0.
*   **Result:** A diagonal element in $R$ becomes 0. If both the process noise $Q$ and measurement noise $R$ for a state variable are 0, and the initial covariance $P$ is also poorly scaled or has zero variance, the innovation covariance matrix $S_k$ can become singular or poorly conditioned, leading to numerical overflow or `NaN` during matrix inversion ($S_k^{-1}$).

---

## 5. NaN Propagation & Missing Values in `extended_kalman_filter()`

If a DataFrame containing rows with missing values (`NaN`) in the measurement columns is passed directly to the `extended_kalman_filter` function, it will trigger a catastrophic NaN propagation.

### 5.1 The Gating & Matrix Multiplication Disconnect
Within `traffic/algorithms/filters/ekf.py`'s `extended_kalman_filter` function, the measurement update loop executes the following steps:

1.  **Innovation Calculation:**
    The innovation vector $\nu$ (residual between measurement and prediction) is computed:
    ```python
    nu = measurement - x
    ```
    If a measurement component $j$ is `NaN` (such as at a newly inserted grid point), the innovation element `nu[j]` evaluates to `NaN`.

2.  **Outlier Rejection (Gating Loop):**
    The filter loops over each state variable to verify if the measurement is a statistical outlier:
    ```python
    for j in range(num_states):
        if abs(nu[j]) > abs(reject_sigma * std_devs[j]):
            measurement.iloc[j] = x.iloc[j]  # Replace faulty measurement
            H[j, j] = 0  # Ignore this component in the update
    ```
    In Python, any comparison involving `NaN` (such as `abs(NaN) > abs(...)`) always evaluates to `False`.
    *   **The Fail-Open Behavior:** Because the comparison is `False`, the `if` block is **skipped**. The measurement component `measurement.iloc[j]` is *not* replaced with the prediction, and the corresponding entry in the observation matrix $H[j, j]$ remains `1` instead of being set to `0` (which would have disabled that measurement channel).

3.  **State Update Matrix Multiplication:**
    The optimal Kalman gain $K$ is computed, and the state vector $x$ is updated:
    ```python
    x = x + K @ nu
    ```
    Since `nu` contains a `NaN` element and $K$ is a dense $6 \times 6$ matrix, the matrix-vector multiplication `K @ nu` propagates the `NaN` to **every single element of the updated state vector $x$**.

4.  **Cascading Failure:**
    Once the state estimate $x$ is written as `NaN` at step $i$, all subsequent prediction steps (e.g. `state_transition_function(x, dt)`) and update steps will receive `NaN` inputs. As a result, **every single waypoint from the first NaN measurement to the end of the flight is permanently wiped to `NaN`**.

---

## 6. Skip Correction Step for Missing Data (Strict Kalman filter math)

To correctly handle missing values in EKF without pre-interpolation, the filter can be modified to skip the correction step (measurement update) for specific variables or entire timestamps when measurements are unavailable.

### 6.1 Skipping the Correction Step for Fully NaN Timestamps
If all measurements at a given timestamp are `NaN`, the correction step can be skipped entirely. This sets the a posteriori state and covariance estimates equal to the a priori prediction:
$$\begin{aligned}
x_k &= x_k^- \\
P_k &= P_k^-
\end{aligned}$$

#### Code Implementation:
In the EKF measurement loop, insert a check at the beginning of the update step to verify if the entire measurement vector is `NaN`:
```python
        # In extended_kalman_filter() loop:
        measurement = measurements.iloc[i]
        
        if measurement.isnull().all():
            # Skip the correction step entirely and accept predicted values
            states[i] = x
            covariances[i] = P
            continue
```

### 6.2 Skipping Correction for Partially NaN Timestamps (Partial Updates)
If only a subset of measurements is missing (e.g. altitude is valid, but horizontal position is `NaN`), the correction step can be adjusted to ignore only the missing variables. This is done by dynamically modifying the innovation vector $\nu$ and the observation matrix $H$:

1.  **Innovation Reset:** For each missing component $j$ where `measurement.iloc[j]` is `NaN`, we set $\nu[j] = 0$.
2.  **Observation Channel Deactivation:** We set the corresponding diagonal entry of the observation matrix $H[j, j] = 0$.
3.  **Update Computation:** The standard Kalman update equations are then run. Since the innovation for $j$ is zero and the observation gain is deactivated, the state variable $j$ relies entirely on the process model prediction, while the valid variables are corrected normally.

#### Code Implementation:
```python
        H = np.eye(num_states)
        nu = measurement - x
        S = H @ P @ H.T + R
        std_devs = np.sqrt(np.diag(S))

        for j in range(num_states):
            if pd.isna(measurement.iloc[j]):
                # Skip correction for this channel
                nu.iloc[j] = 0.0
                H[j, j] = 0.0
            elif abs(nu[j]) > abs(reject_sigma * std_devs[j]):
                measurement.iloc[j] = x.iloc[j]
                H[j, j] = 0
```
This is mathematically rigorous and prevents NaN propagation while correctly leveraging partial measurements.
