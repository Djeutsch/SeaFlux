

def area_grid(resolution=1):
    """Calculate the area of each grid cell for a user-provided
    grid cell resolution. Area is in square meters, but resolution
    is given in decimal degrees."""
    import numpy as np
    import xarray as xr

    # Calculations needs to be in radians
    lats = np.deg2rad(np.arange(-90, 90.1, resolution))
    r_sq = 6371000 ** 2
    n_lats = int(360.0 / resolution)
    area = (
        r_sq
        * np.ones(n_lats)[:, None]
        * np.deg2rad(resolution)
        * (np.sin(lats[1:]) - np.sin(lats[:-1]))
    )
    xda = xr.DataArray(
        area.T,
        dims=["lat", "lon"],
        coords={
            "lat": np.arange(-90 + 0.5, 90),
            "lon": np.arange(-180 + 0.5, 180),
        },
        attrs={
            "long_name": "area_per_pixel",
            "description": "area per pixel",
            "units": "m^2",
        },
    )

    return xda


def noaa_mbl_to_dataset(
    noaa_mbl_url,
    target_lat=None,
    target_lon=None,
    interp_method='linear',
):
    """
    Downloads the NOAA marine boundary layer xCO2 and grids it
    to a defined grid if target lat and lon provided

    Parameters
    ==========
    noaa_mbl_url: str
        see this site for more details and click surface for download
        https://www.esrl.noaa.gov/gmd/ccgg/mbl/index.html
    target_lat: None | array-like
        if None, the default lats will be returned, if array-like
        then will return xCO2 interpolated onto the given latitudes
    target_lon: None | array-like
        if None, data will not be broadcast (expanded) along latitudes,
        if array-like, then will broadcast to those longitudes.
    inter_method: linear|nearest
        the interpolation type passed to xr.DataArray.interp. Accepted
        options are linear|nearest. MPI-SOMFFN uses nearest, I prefer
        linear
    """
    from pandas import Timestamp
    import xarray as xr
    import numpy as np

    def download_and_read_noaa_mbl(noaa_mbl_url):
        import re
        import pooch
        import pandas as pd

        # save to temporary location with pooch
        fname = pooch.retrieve(noaa_mbl_url, None)

        # find start line
        is_mbl_surface = False
        for start_line, line in enumerate(open(fname)):
            if re.findall('MBL.*SURFACE', line):
                is_mbl_surface = True
            if not line.startswith('#'):
                break
        if not is_mbl_surface:
            raise Exception(
                'The file at the provided url is not an MBL SURFACE file. '
                'Please check that you have provided the surface url. '
            )

        # read fixed width file CO2
        df = pd.read_fwf(fname, skiprows=start_line, header=None, index_col=0)
        df.index.name = 'date'
        # every second line is uncertainty
        df = df.iloc[:, ::2]
        # latitude is given as sin(lat)
        df.columns = np.rad2deg(np.arcsin(np.linspace(-1, 1, 41)))

        # resolve time properly
        year = (df.index.values - (df.index.values % 1)).astype(int)
        day_of_year = ((df.index.values - year) * 365 + 1).astype(int)
        date_strings = ['{}-{:03d}'.format(*a) for a in zip(year, day_of_year)]
        date = pd.to_datetime(date_strings, format='%Y-%j')
        df = df.set_index(date)

        # renaming indexes (have to stack for that)
        df = df.stack()
        index = df.index.set_names(['time', 'lat'])
        df = df.set_axis(index)

        df.source = noaa_mbl_url

        return df

    history = (
        f'[SeaFlux@{Timestamp.today():%Y-%m-%dT%H:%M}]: '
        f'downloaded NOAA MBL data from {noaa_mbl_url}, ')

    df = download_and_read_noaa_mbl(noaa_mbl_url)
    xda = df.to_xarray()

    if target_lat is not None:
        history += f'latitude interpolated with {interp_method}, '
        xda = xda.interp(lat=target_lat, method=interp_method)

    if target_lon is not None:
        history += f'longitude broadcast'
        lon = xr.DataArray(
            np.ones_like(target_lon),
            dims=['lon'],
            coords=[target_lon]
        )
        xda = xda * lon

    xda.attrs = dict(
        units='ppm',
        product='NOAA Greenhouse Gas Marine Boundary Layer Reference',
        history=history,
        source='https://www.esrl.noaa.gov/gmd/ccgg/mbl/index.html',
        description=(
            'mole fraction of CO2 for the marine boundary layer varying by '
            'latitude and time. Note that values are constant along '
            'longitudes. '))

    return xda


def noaa_mbl_to_pCO2(noaa_mbl_url, press_hPa, tempSW_C, salt):
    """
    This is a high-level function that downloads xCO2 (noaa_mbl_url) and
    calculates pCO2 for the given inputs. These need to be xarray.DataArrays
    of the same shape

    Parameters
    ----------
    noaa_mbl_url: string
        the download link for surface data from
        https://www.esrl.noaa.gov/gmd/ccgg/mbl/index.html
    press_hPa: xr.DataArray
        mean sea level pressure for the global ocean
        (use ERA5 for global estimates)
    tempSW_C: xr.DataArray
        sea surface temperature
        (I recommend SODA or EN4 for global values)
    salt: xr.DataArray
        sea surface salinity (or analogous)

    Returns
    -------
    pCO2mbl : xr.DataArray
        pCO2 for the marine boundary layer assuming with the same shape
        as the input xarrays

    """
    from .core import atm_xCO2_to_pCO2
    from pandas import Timestamp
    from xarray import DataArray

    def all_same(items):
        return all(x == items[0] for x in items)

    inputs = [press_hPa, tempSW_C, salt]
    types = [isinstance(a, DataArray) for a in inputs]
    assert all(types), 'All input arrays must be xr.DataArrays'
    shapes = {a.name: a.shape for a in inputs}
    assert all_same(shapes.values()), f'all inputs shapes must match\n{shapes}'
    dims = [d in ('lat', 'lon') for d in press_hPa.dims]
    assert all(dims), 'lat/lon must be dimensions of input arrays'

    xCO2atm = noaa_mbl_to_dataset(
        noaa_mbl_url,
        target_lat=press_hPa.lat.values,
        target_lon=press_hPa.lon.values,
    )

    pCO2atm = atm_xCO2_to_pCO2(xCO2atm, press_hPa, tempSW_C, salt)

    pCO2atm.name = 'pCO2atm_MBLnoaa'
    pCO2atm.attrs = dict(
        standard_name=(
            'partial_pressure_of_carbon_dioxide_in_the_marine_boundary_layer'
        ),
        short_name='pCO2mbl',
        units='uatm',
        description=(
            'Atmospheric pCO2 for the marine boundary layer is calculated '
            'from the NOAAs marine boundary layer pCO2 with: xCO2 * (Patm '
            '- pH2O). Where pH2O is calculated using vapour pressure from '
            'Dickson et al. (2007)'),
        history=(
            getattr(xCO2atm, 'history', '').strip(';') + ';\n'
            f'[SeaFlux@{Timestamp.today():%Y-%m-%dT%H:%M}]: '
            f'pCO2 calculated from xCO2 * (Patm - pH2O), where '
            f'pH2O is calculated with Dickson et al. (2007)'),
        citation=(
            'Ed Dlugokencky and Pieter Tans, NOAA/ESRL '
            '(www.esrl.noaa.gov/gmd/ccgg/trends/)'))

    return pCO2atm
