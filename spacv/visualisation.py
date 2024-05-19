import logging
import multiprocessing
from typing import Tuple, Union

import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.axis import Axis
from matplotlib.colors import ListedColormap
from matplotlib.figure import Figure
from scipy.optimize import curve_fit
from scipy.spatial.distance import pdist, squareform
from sklearn.metrics.pairwise import haversine_distances
from sklearn.neighbors import BallTree

from .utils import geometry_to_2d

try:
    import seaborn as sns

    sns.set_style("whitegrid")  # pretty plots
except ModuleNotFoundError:
    pass

__all__ = [
    "variogram_at_lag",
    "compute_semivariance",
    "plot_autocorrelation_ranges",
    "aoa",
    "plot_aoa",
]


def variogram_at_lag(
    XYs: gpd.GeoSeries,
    x: Union[list, np.ndarray, gpd.GeoSeries],
    lags: np.ndarray,
    bw: Union[int, float],
    distance_metric: str = "euclidean",
    col_name: str = None,
) -> np.ndarray:
    """
    Return semivariance values for defined lag of distance.

    Parameters
    ----------
    XYs : Geoseries series
        Series containing X and Y coordinates.
    X : array, list, or Geoseries
        Array (N,) containing variable.
    lags : array
        Array of distance lags in metres to obtain semivariances.
    bw : integer or float
        Bandwidth, plus and minus lags to calculate semivariance.
    distance_metric : string
        Distance function to calculate pairwise distances. Must be "euclidean" for
        points in euclidean space, or "haversine" for points in a geographic CRS.
        Defaults to "euclidean".

    Returns
    -------
    semivariances : Array of floats
        Array of semivariances at defined lag points for given variable.
    """
    XYs = geometry_to_2d(XYs)
    x = np.asarray(x)

    if distance_metric == "euclidean":
        paired_distances = pdist(XYs)
        pd_m = squareform(paired_distances)
    elif distance_metric == "haversine":
        pd_m = haversine_distances(np.radians([*XYs]))
        pd_m = pd_m * 6371000  # multiply by Earth radius to get meters

    semivariances = np.empty((len(lags)), dtype=np.float64)

    for i, lag in enumerate(lags):
        # Mask pts outside bandwidth
        lower = pd_m >= lag - bw
        upper = pd_m <= lag + bw
        mask = np.logical_and(lower, upper)
        semivariances[i] = compute_semivariance(x, mask, bw, col_name)

    return np.c_[semivariances, lags].T


def compute_semivariance(x, mask, bw, col_name):
    """
    Calculate semivariance for masked elements.
    """
    semis, counts = [], []
    for i in range(len(x)):
        xi = np.array([x[i]])
        mask_i = mask[i, :]
        mask_i[:i] = False
        xj = x[mask_i]
        ss = (xi - xj) ** 2
        filter_empty = ss > 0.0
        if len(ss[filter_empty]) > 0:
            counts.append(len(ss[filter_empty]))
            semis.append(ss[filter_empty])
    try:
        semivariance = np.sum(np.concatenate(semis)) / (2.0 * sum(counts))
        return semivariance
    except ValueError:
        logging.error(
            f"Could not calculate semivariances for {col_name}. Using 0 instead."
        )
        return 0


def variogram(func):
    def send_params(*params):
        new_args = params[1:]
        mapping = map(lambda h: func(h, *new_args), params[0])
        return np.fromiter(mapping, dtype=float)

    return send_params


@variogram
def spherical(h, r, sill, nugget=0):
    """
    Spherical variogram model function. Calculates the
    dependent variable for a given lag (h). The nugget (b) defaults to be 0.

    Parameters
    ----------
    h : float
        The lag at which the dependent variable is calculated at.
    r : float
        Effective range of autocorrelation.
    sill : float
        The sill of the variogram, where the semivariance begins to saturate.
    nugget : float, default=0
        The nugget of the variogram. This is the value of independent
        variable at the distance of zero.
    Returns
    -------
    gamma : numpy float
        Coefficients that describe effective range of spatial autocorrelation
    """
    a = r / 1.0
    if h <= r:
        return nugget + sill * ((1.5 * (h / a)) - (0.5 * ((h / a) ** 3.0)))
    else:
        return nugget + sill


def calculate_range(args):
    XYs, col, lags, bw, distance_metric, col_name = args
    semis = variogram_at_lag(XYs, col, lags, bw, distance_metric, col_name)
    sv, h = semis[0], semis[1]
    start_params = [np.nanmax(h), np.nanmax(sv)]
    bounds = (0, start_params)
    cof, _ = curve_fit(
        spherical, h, sv, sigma=None, p0=start_params, bounds=bounds, method="trf"
    )
    effective_range = cof[0]
    return effective_range


def plot_autocorrelation_ranges(
    XYs: gpd.GeoSeries,
    X: Union[np.ndarray, gpd.GeoDataFrame],
    lags: np.ndarray,
    bw: Union[int, float],
    distance_metric: str = "euclidean",
    workers: int = 1,
    verbose: bool = False,
    **kwargs,
) -> Tuple[Figure, Axis, list]:
    """
    Plot spatial autocorrelation ranges for input covariates. Suggested
    block size is proposed by taking the median autocorrelation range
    across the data and is reported by the horiztonal line. This function
    works best for projected coordinate systems.

    Parameters
    ----------
    XYs : Geoseries series
        Series containing X and Y coordinates.
    X : array or dataframe
        Dataframeof covariates to calculate autocorrelation ranges over.
    lags : array
        Array of distance lags in metres to obtain semivariances.
    bw : integer or float
        Bandwidth, plus and minus lags to calculate semivariance.
    distance_metric : string
        Distance function to calculate pairwise distances. Must be "euclidean" for
        points in euclidean space, or "haversine" for points in a geographic CRS.
        Defaults to "euclidean".
    workers : int
        Use multiprocessing for >1 worker. -1 uses all available cores. Defaults to 1.
    verbose : bool
        Print name of current column being processed.

    Returns
    -------
    fig : matplotlip Figure instance
        Figure of spatial weight network.
    ax : matplotlib Axes instance
        Axes in which the figure is plotted.
    ranges : list
    """
    alpha = kwargs.pop("alpha", 0.7)
    font_size = kwargs.pop("font_size", 14)
    block_suggestion_color = kwargs.pop("color", "red")
    figsize = kwargs.pop("figsize", (8, 6))

    ranges = []

    if workers == -1 or workers > 1:
        pool = multiprocessing.Pool(workers)
        results = []

        for i, col in enumerate(X.values.T):
            col_name = X.columns[i]
            if verbose:
                print(f"{i}: {col_name}")
            # Fit spherical model and extract effective range parameter
            args = (XYs, col, lags, bw, distance_metric, col_name)
            results.append(pool.apply_async(calculate_range, (args,)))

        for result in results:
            ranges.append(result.get())

        pool.close()
        pool.join()
    else:
        for i, col in enumerate(X.values.T):
            col_name = X.columns[i]
            if verbose:
                print(f"{i}: {col_name}")
            # Fit spherical model and extract effective range parameter
            args = (XYs, col, lags, bw, distance_metric, col_name)
            eff_range = calculate_range(args)
            ranges.append(eff_range)

    x_labs = X.columns
    f, ax = plt.subplots(1, figsize=figsize)
    ax.bar(x_labs, ranges, color="skyblue", alpha=alpha)
    ax.set_ylabel("Ranges (m)")
    ax.set_xlabel("Variables")
    median_eff_range = np.median(ranges)
    ax.text(
        0,
        median_eff_range + (np.max(ranges) / 100 * 3),
        "{:.3f}m".format(median_eff_range),
        color=block_suggestion_color,
        size=font_size,
    )
    ax.axhline(median_eff_range, color=block_suggestion_color, linestyle="--")
    return f, ax, ranges


def aoa(
    new_data,
    training_data,
    model=None,
    thres=0.95,
    fold_indices=None,
    distance_metric="euclidean",
):
    """
    Area of Applicability (AOA) measure for spatial prediction models from
    Meyer and Pebesma (2020). The AOA defines the area for which, on average,
    the cross-validation error of the model applies, which is crucial for
    cases where spatial predictions are used to inform decision-making.

    Parameters
    ----------
    new_data : GeoDataFrame
        A GeoDataFrame containing unseen data to measure AOA for.
    training_data : GeoDataFrame
        A GeoDataFrame containing the features used for model training.
    thres : default=0.95
        Threshold used to identify predictive area of applicability.
    fold_indices : iterable, default=None
        iterable consisting of training indices that identify instances in the
        folds.
    distance_metric : string, default='euclidean'
        Distance metric to calculate distances between new_data and training_data.
        Defaults to euclidean for projected CRS, otherwise haversine for unprojected.
    Returns
    -------
    DIs : array
        Array of disimimilarity scores between training_data for new_data points.
    masked_result : array
        Binary mask that occludes points outside predictive area of applicability.
    """
    if len(training_data) <= 1:
        raise Exception("At least two training instances need to be specified.")

    # Scale data
    training_data = (training_data - np.mean(training_data)) / np.std(training_data)
    new_data = (new_data - np.mean(new_data)) / np.std(new_data)

    # Calculate nearest training instance to test data, return Euclidean distances
    tree = BallTree(training_data, metric=distance_metric)
    mindist, _ = tree.query(new_data, k=1, return_distance=True)

    # Build matrix of pairwise distances
    paired_distances = pdist(training_data)
    train_dist = squareform(paired_distances)
    np.fill_diagonal(train_dist, np.nan)

    # Remove data points that are within the same fold
    if fold_indices:
        # Get number of training instances in each fold
        instances_in_folds = [len(fold) for fold in fold_indices]
        instance_fold_id = np.repeat(
            np.arange(0, len(fold_indices)), instances_in_folds
        )

        # Create mapping between training instance and fold ID
        fold_indices = np.concatenate(fold_indices)
        folds = np.vstack((fold_indices, instance_fold_id)).T

        # Mask training points in same fold for DI measure calculation
        for i, row in enumerate(train_dist):
            mask = folds[:, 0] == folds[:, 0][i]
            train_dist[i, mask] = np.nan

    # Scale distance to nearest training point by average distance across training data
    train_dist_mean = np.nanmean(train_dist, axis=1)
    train_dist_avgmean = np.mean(train_dist_mean)
    mindist /= train_dist_avgmean

    # Define threshold for AOA
    train_dist_min = np.nanmin(train_dist, axis=1)
    # aoa_train_stats = np.quantile(
    #     train_dist_min / train_dist_avgmean,
    #     q=np.array([0.25, 0.5, 0.75, 0.9, 0.95, 0.99, 1]),
    # )
    thres = np.quantile(train_dist_min / train_dist_avgmean, q=thres)

    # We choose the AOA as the area where the DI does not exceed the threshold
    DIs = mindist.reshape(-1)
    masked_result = np.repeat(1, len(mindist))
    masked_result[DIs > thres] = 0

    return DIs, masked_result


def plot_aoa(
    new_data, training_data, columns, figsize=(14, 4), fold_indices=None, **kwargs
):
    """
     Parameters
    ----------
    new_data : GeoDataFrame
        A GeoDataFrame containing unseen data to measure AOA for.
    training_data : GeoDataFrame
        A GeoDataFrame containing the features used for model training.
    columns : array
        Column names of variables used to assess disimilarity between
        new_data and training_data.
    figsize : tuple, default=(14,4)
        Width, height of figure in inches.
    fold_indices : iterable, default=None
        Iterable consisting of training indices that identify instances in the
        folds.

    Returns
    -------
    fig : matplotlip Figure instance
        Figure of spatial weight network.
    ax : matplotlib Axes instance
        Axes in which the figure is plotted.
    """
    # Pop geometry for use later in plotting
    new_data = new_data.copy()
    new_data_geometry = new_data.pop("geometry")

    # Subset to variables
    new_data_aoa = new_data[columns]
    training_data_aoa = training_data[columns]

    DIs, masked_result = aoa(new_data_aoa, training_data_aoa, fold_indices=fold_indices)

    new_data.loc[:, "DI"] = DIs
    new_data.loc[:, "AOA"] = masked_result
    new_data.loc[:, "geometry"] = new_data_geometry
    new_data = gpd.GeoDataFrame(new_data, geometry=new_data["geometry"])

    f, ax = plt.subplots(1, 2, figsize=figsize)

    new_data.plot(ax=ax[0], column="DI", legend=True, cmap="viridis")
    new_data.plot(
        ax=ax[1],
        column="AOA",
        categorical=True,
        legend=True,
        cmap=ListedColormap(["red", "blue"]),
    )
    training_data.plot(ax=ax[0], alpha=0.3)
    training_data.plot(ax=ax[1], alpha=0.3)

    ax[0].set_aspect("auto")
    ax[1].set_aspect("auto")
    ax[0].set_title("Dissimilarity index (DI)")
    ax[1].set_title("AOA")

    return f, ax


def plot_variogram(
    XYs: gpd.GeoSeries,
    col: Union[gpd.GeoSeries, np.ndarray],
    lags: np.ndarray,
    bw: Union[int, float],
    distance_metric: str = "euclidean",
) -> np.ndarray:
    """
    Return semivariance values for defined lag of distance.

    Parameters
    ----------
    XYs : Geoseries series
        Series containing X and Y coordinates.
    col : array or list
        Array (N,) containing variable.
    lags : array
        Array of distance lags in metres to obtain semivariances.
    bw : integer or float
        Bandwidth, plus and minus lags to calculate semivariance.
    distance_metric : string
        Distance function to calculate pairwise distances. Must be "euclidean" for
        points in euclidean space, or "haversine" for points in a geographic CRS.
        Defaults to "euclidean".

    Returns
    -------
    semis : Array of floats
        Array of semivariances at defined lag points for given variable.
    """
    semis = variogram_at_lag(XYs, col, lags, bw, distance_metric)
    sv, h = semis[0], semis[1]
    plt.scatter(h, sv)
    plt.show()

    return semis
