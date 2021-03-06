import logging
import numpy
import pandas
import typing

import arviz
import pymc3
import theano
import theano.tensor as tt
import theano.tensor.signal.conv
import xarray


__version__ = '1.1.0'
_log = logging.getLogger(__file__)


def _reindex_observed(observed:pandas.DataFrame, buffer_days:int=10):
    _log.info("Model will start with %i unobserved buffer days before the data.", buffer_days)
    first_index = observed.new_cases.gt(0).argmax()
    observed = observed.iloc[first_index:]
    new_index = pandas.date_range(
        start=observed.index[0] - pandas.Timedelta(days=buffer_days),
        end=observed.index[-1],
        freq="D",
    )
    observed = observed.reindex(new_index, fill_value=numpy.nan)
    return observed


def _to_convolution_ready_gt(generation_time, len_observed):
    """ Speeds up theano.scan by pre-computing the generation time interval
        vector. Thank you to Junpeng Lao for this optimization.
        Please see the outbreak simulation math here:
        https://staff.math.su.se/hoehle/blog/2020/04/15/effectiveR0.html """
    convolution_ready_gt = numpy.zeros((len_observed - 1, len_observed))
    for t in range(1, len_observed):
        begin = numpy.maximum(0, t - len(generation_time) + 1)
        slice_update = generation_time[1 : t - begin + 1][::-1]
        convolution_ready_gt[
            t - 1, begin : begin + len(slice_update)
        ] = slice_update
    convolution_ready_gt = theano.shared(convolution_ready_gt)
    return convolution_ready_gt


def build_model(
    observed:pandas.DataFrame,
    p_generation_time:numpy.ndarray,
    p_delay:numpy.ndarray,
    test_col:str,
    buffer_days:int=10,
    pmodel:typing.Optional[pymc3.Model]=None,
) -> pymc3.Model:
    """ Builds the Rt.live PyMC3 model.

    Model by Kevin Systrom, Thomas Vladek and Rtlive contributors.

    Parameters
    ----------
    observed : pandas.DataFrame
        date-indexed dataframe with column "new_cases" (daily positives) 
        and a column of daily tests whose name is specified by parameter [test_col]
    p_generation_time : numpy.ndarray
        numpy array that describes the generation time distribution
    p_delay : numpy.ndarray
        numpy array that describes the testing delay distribution
    test_col : str
        name of column with daily new tests (predicted or actual data)
    buffer_days : int
        number of days to prepend before the beginning of the data
    pmodel : optional, PyMC3 model
        an existing PyMC3 model object to use (not context-activated)

    Returns
    -------
    pmodel : pymc3.Model
        the (created) PyMC3 model
    """
    observed = observed.rename(columns={test_col: "daily_tests"})
    # Reindex to make sure that there are no gaps.
    # Also add (unobserved) buffer days at the beginning.
    observed = _reindex_observed(observed, buffer_days)

    # make boolean masks to filter for dates that have case data, testcount data or both
    has_cases = ~numpy.isnan(observed.new_cases).values
    has_testcounts = ~numpy.isnan(observed.daily_tests).values
    has_data = has_cases & has_testcounts
    # masks that can be used w.r.t. subsets of the dates.
    # These are used to slice tensors that are already shorter than the full length.
    has_data_wrt_cases = has_data[has_cases]
    has_data_wrt_testcounts = has_data[has_testcounts]

    coords = {
        # this is the full lenght of dates (without gaps) covered by the generative part of the model
        "date": observed.index.values,
        # these are subsets of dates where case/testcount data is available
        "date_with_cases": observed.index.values[has_cases],
        "date_with_testcounts": observed.index.values[has_testcounts],
        # and the dates with both case & testcount data (for the likelihood)
        "date_with_data": observed.index.values[has_data],
    }
    N_dates = len(coords["date"])
    N_with_cases = len(coords["date_with_cases"])
    N_with_testcounts = len(coords["date_with_testcounts"])
    N_with_data = len(coords["date_with_data"])
    _log.info(
        "The model describes %i days of which %i have case data and %i have testcount data. %i days have both.",
        N_dates, N_with_cases, N_with_testcounts, N_with_data
    )

    if not pmodel:
        pmodel = pymc3.Model(coords=coords)

    with pmodel:
        # Let log_r_t walk randomly with a fixed prior of ~0.035. Think
        # of this number as how quickly r_t can react.
        log_r_t = pymc3.GaussianRandomWalk(
            "log_r_t",
            sigma=0.035,
            dims=["date"]
        )
        r_t = pymc3.Deterministic("r_t", pymc3.math.exp(log_r_t), dims=["date"])

        # Save data as part of trace so we can access in inference_data
        t_generation_time = pymc3.Data("p_generation_time", p_generation_time)
        # precompute generation time interval vector to speed up tt.scan
        convolution_ready_gt = _to_convolution_ready_gt(p_generation_time, N_dates)
        # For a given seed population and R_t curve, we calculate the
        # implied infection curve by simulating an outbreak. While this may
        # look daunting, it's simply a way to recreate the outbreak
        # simulation math inside the model:
        # https://staff.math.su.se/hoehle/blog/2020/04/15/effectiveR0.html
        seed = pymc3.Exponential("seed", 1 / 0.02)
        y0 = tt.zeros(N_dates)
        y0 = tt.set_subtensor(y0[0], seed)
        outputs, _ = theano.scan(
            fn=lambda t, gt, y, r_t: tt.set_subtensor(y[t], tt.sum(r_t * y * gt)),
            sequences=[tt.arange(1, N_dates), convolution_ready_gt],
            outputs_info=y0,
            non_sequences=r_t,
            n_steps=N_dates - 1,
        )
        infections = pymc3.Deterministic("infections", outputs[-1], dims=["date"])

        t_p_delay = pymc3.Data("p_delay", p_delay)
        # Convolve infections to confirmed positive reports based on a known
        # p_delay distribution. See patients.py for details on how we calculate
        # this distribution.
        test_adjusted_positive = pymc3.Deterministic(
            "test_adjusted_positive",
            theano.tensor.signal.conv.conv2d(
                tt.reshape(infections, (1, N_dates)),
                tt.reshape(t_p_delay, (1, len(p_delay))),
                border_mode="full",
            )[0, :N_dates],
            dims=["date"]
        )

        # Picking an exposure with a prior that exposure never goes below
        # 0.1 * max_tests. The 0.1 only affects early values of Rt when
        # testing was minimal or when data errors cause underreporting
        # of tests.
        tests = pymc3.Data("tests", observed.daily_tests[has_testcounts], dims=["date_with_testcounts"])
        exposure = pymc3.Deterministic(
            "exposure",
            pymc3.math.clip(tests, observed.daily_tests.max() * 0.1, 1e9),
            dims=["date_with_testcounts"]
        )

        # Test-volume adjust reported cases based on an assumed exposure
        # Note: this is similar to the exposure parameter in a Poisson
        # regression.
        positive = pymc3.Deterministic(
            "positive", exposure * test_adjusted_positive[has_testcounts],
            dims=["date_with_testcounts"]
        )
        positive_where_data = pymc3.Deterministic("positive_where_data", positive[has_data_wrt_testcounts], dims=["date_with_data"])

        observed_positive = pymc3.Data("observed_positive", observed.new_cases[has_cases], dims=["date_with_cases"])
        observed_positive_where_data = pymc3.Data("observed_positive_where_data", observed.new_cases[has_cases][has_data_wrt_cases], dims=["date_with_data"])

        likelihood = pymc3.NegativeBinomial(
            "likelihood",
            mu=positive_where_data,
            alpha=pymc3.Gamma("alpha", mu=6, sigma=1),
            observed=observed_positive_where_data,
            dims=["date_with_data"]
        )
    return pmodel


def sample(pmodel:pymc3.Model, **kwargs):
    """ Run sampling with default settings.

    Parameters
    ----------
    pmodel : pymc3.Model
        the PyMC3 model to sample from
    **kwargs
        additional keyword-arguments to pass to pm.sample
        (overriding the defaults from this implementation)

    Returns
    -------
    idata : arviz.InferenceData
        the sampling and posterior predictive result
    """
    with pmodel:
        sample_kwargs = dict(
            return_inferencedata=False,
            target_accept=0.95,
            init='jitter+adapt_diag',
            cores=4,
            chains=4,
            tune=700, draws=200,
        )
        sample_kwargs.update(kwargs)
        trace = pymc3.sample(**sample_kwargs)

        idata = arviz.from_pymc3(
            trace=trace,
            posterior_predictive=pymc3.sample_posterior_predictive(trace),
        )
        idata.posterior.attrs["model_version"] = __version__
    return idata


def get_scale_factor(idata: arviz.InferenceData) -> xarray.DataArray:
    """ Calculate a scaling factor so we can work/plot with
    the inferred "infections" curve.

    The scaling factor depends on the probability that an infection is observed
    (sum of p_delay distribution). The current p_delay distribution sums to 0.9999999,
    so right now the scaling ASSUMES THAT THERE'S NO DARK FIGURE !!
    Therefore the factor should be interpreted as the lower-bound!!

    Parameters
    ----------
    idata : arviz.InferenceData
        sampling result of Rtlive model v1.0.2 or higher

    Returns
    -------
    factor : xarray.DataArray
        scaling factors (sample,)
    """
    # coords changed with model v1.1.0. This ensure backwards-compatibility.
    coord_tap = idata.posterior.test_adjusted_positive.coords.dims[-1]
    coord_exposure = idata.constant_data.exposure.coords.dims[-1]
    coord_observed_positive = idata.constant_data.observed_positive.coords.dims[-1]

    # the scaling factor is calculated from a comparison between
    # (test_adjusted_positive * exposure) vs. sum(observed_positive)
    # but only the dates where both case and test count are available must be considered

    coord_date_with_data = tuple(idata.observed_data.coords.dims)[0]
    date_with_data = set(tuple(idata.observed_data[coord_date_with_data].values))

    mask_tap = [
        d in date_with_data
        for d in idata.posterior[coord_tap].values
    ]
    mask_exposure = [
        d in date_with_data
        for d in idata.posterior[coord_exposure].values
    ]
    mask_observed = [
        d in date_with_data
        for d in idata.constant_data[coord_observed_positive].values
    ]

    test_adjusted_positive = idata.posterior.test_adjusted_positive[:, :, mask_tap].rename({coord_tap: "date_with_data"})
    exposure = idata.posterior.exposure[:, :, mask_exposure].rename({coord_exposure: "date_with_data"})
    exposure_profile = exposure / idata.constant_data.exposure.max()

    total_observed = idata.constant_data.observed_positive[mask_observed].sum(coord_observed_positive)
    total_inferred = (test_adjusted_positive  * exposure_profile) \
        .stack(sample=('chain', 'draw')) \
        .sum('date_with_data')
    p_observe = numpy.sum(idata.constant_data.p_delay)

    scale_factor = total_observed / total_inferred / p_observe
    return scale_factor


def get_case_curves(idata: arviz.InferenceData) -> typing.Tuple[xarray.DataArray, xarray.DataArray, xarray.DataArray]:
    """ Calculates curves of daily new cases, total cases and active cases
    from a sampling result.

    Parameters
    ----------
    idata : arviz.InferenceData
        the sampling result

    Returns
    -------
    new_cases : xarray.DataArray
        curve distribution of daily new cases
    total_cases : xarray.DataArray
        curve distribution of cumulative cases
    active_cases : xarray.DataArray
        curve distribution of actively infectious cases.
        Weights the cases by their probability of being infectious
        as implied by the generation time distribution.
    """
    # start from the posterior of (normalized) daily new infections
    infections = idata.posterior.infections.stack(sample=('chain', 'draw'))
    days, samples = infections.shape
    # scaled up to actual numbers
    scale_factor = get_scale_factor(idata)
    new_cases = infections * scale_factor

    # total case count is just a cumulative sum
    total_cases = numpy.cumsum(new_cases, axis=0)
    assert total_cases.shape == (days, samples)
    total_cases = xarray.DataArray(total_cases, coords={
        'date': idata.posterior.date.values,
        'sample': numpy.arange(total_cases.shape[1])
    }, dims=('date', 'sample'))

    # number of active cases over time depends on the generation time, which
    # we can re-interpret into a probability of a case being active (decays over time)
    p_active = 1 - numpy.cumsum(idata.constant_data.p_generation_time.values)
    # convolution of the above gives a curve of active cases
    convolve = numpy.vectorize(numpy.convolve, signature='(n),(m)->(k)')
    active_cases = convolve(new_cases.T, p_active).T[:days, :]
    assert active_cases.shape == (days, samples), active_cases.shape
    assert active_cases.shape == (days, samples)
    active_cases = xarray.DataArray(active_cases, coords={
        'date': idata.posterior.date.values,
        'sample': numpy.arange(active_cases.shape[1])
    }, dims=('date', 'sample'))

    return new_cases, total_cases, active_cases
