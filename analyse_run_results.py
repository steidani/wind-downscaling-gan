import os
from pathlib import Path

import cartopy
import cartopy.crs as ccrs
import matplotlib.gridspec as gridspec
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import xarray as xr
from cartopy.crs import epsg

crs_cosmo = epsg(21781)

from silence_tensorflow import silence_tensorflow
from data.data_processing import HigherResPlateCarree
from gan.metrics import WindSpeedRMSE, WindSpeedWeightedRMSE, WeightedRMSEForExtremes, LogSpectralDistance, \
    SpatialKS, AngularCosineDistance

silence_tensorflow()

from data.data_generator import BatchGenerator, NaiveDecoder, LocalFileProvider, S3FileProvider
from data.data_generator import FlexibleNoiseGenerator
from gan import train, metrics
from gan.ganbase import GAN
from gan.models import make_generator, make_discriminator, make_generator_no_noise

TEST_METRICS = [WindSpeedRMSE, WindSpeedWeightedRMSE, WeightedRMSEForExtremes,
                LogSpectralDistance, SpatialKS, AngularCosineDistance]
DATA_ROOT = Path(os.getenv('DATA_ROOT', './data'))
PROCESSED_DATA_FOLDER = DATA_ROOT / 'img_prediction_files'


def get_network_from_config(len_inputs=2,
                            len_outputs=2,
                            sequence_length=6,
                            img_size=256,
                            batch_size=8,
                            noise_channels=100,
                            gen_only=False
                            ):
    if not gen_only:
        # Creating GAN
        generator = make_generator(image_size=img_size, in_channels=len_inputs,
                                   noise_channels=noise_channels, out_channels=len_outputs,
                                   n_timesteps=sequence_length)
        print(f"Generator: {generator.count_params():,} weights")
        discriminator = make_discriminator(low_res_size=img_size, high_res_size=img_size,
                                           low_res_channels=len_inputs,
                                           high_res_channels=len_outputs, n_timesteps=sequence_length)
        print(f"Discriminator: {discriminator.count_params():,} weights")
        noise_shape = (batch_size, sequence_length, img_size, img_size, noise_channels)
        gan = GAN(generator, discriminator, noise_generator=FlexibleNoiseGenerator(noise_shape, std=1))
        print(f"Total: {gan.generator.count_params() + gan.discriminator.count_params():,} weights")
        gan.compile(generator_optimizer=train.generator_optimizer(),
                    generator_metrics=[metrics.AngularCosineDistance(),
                                       metrics.LogSpectralDistance(),
                                       metrics.WeightedRMSEForExtremes(),
                                       metrics.WindSpeedWeightedRMSE(),
                                       metrics.SpatialKS()],
                    discriminator_optimizer=train.discriminator_optimizer(),
                    discriminator_loss=train.discriminator_loss,
                    metrics=[metrics.discriminator_score_fake(), metrics.discriminator_score_real()])
        return gan

    else:
        generator = make_generator_no_noise(image_size=img_size, in_channels=len_inputs,
                                            out_channels=len_outputs,
                                            n_timesteps=sequence_length)
        print(f"Generator: {generator.count_params():,} weights")
        generator.compile(optimizer=train.generator_optimizer(),
                          metrics=[metrics.AngularCosineDistance(),
                                   metrics.LogSpectralDistance(),
                                   metrics.WeightedRMSEForExtremes(),
                                   metrics.WindSpeedWeightedRMSE(),
                                   metrics.SpatialKS()],
                          loss=train.generator_loss)
        return generator


def get_data_providers(data_provider='local', cosmoblurred=False):
    input_pattern = 'x_cosmo_{date}.nc' if cosmoblurred else 'x_{date}.nc'
    if data_provider == 'local':
        input_provider = LocalFileProvider(PROCESSED_DATA_FOLDER, input_pattern)
        output_provider = LocalFileProvider(PROCESSED_DATA_FOLDER, 'y_{date}.nc')
    elif data_provider == 's3':
        input_provider = S3FileProvider('wind-downscaling', 'img_prediction_files', pattern=input_pattern)
        output_provider = S3FileProvider('wind-downscaling', 'img_prediction_files', pattern='y_{date}.nc')
    else:
        raise ValueError(f'Wrong value for data provider {data_provider}: please choose between s3 and local')

    return input_provider, output_provider


def plot_prediction_by_batch(run_id, start_date, end_date, sequence_length=6,
                             img_size=128,
                             batch_size=16,
                             noise_channels=20,
                             cosmoblurred=False,
                             batch_workers=None,
                             data_provider: str = 'local',
                             saving_frequency=10,
                             nb_epochs=500,
                             gen_only=False
                             ):
    TOPO_PREDICTORS = ['tpi_500', 'slope', 'aspect']
    HOMEMADE_PREDICTORS = ['e_plus', 'e_minus', 'w_speed', 'w_angle']
    ERA5_PREDICTORS_SURFACE = ['u10', 'v10', 'blh', 'fsr', 'sp', 'sshf']
    ERA5_PREDICTORS_Z500 = ['z']
    ALL_OUTPUTS = ['U_10M', 'V_10M']
    if batch_workers is None:
        batch_workers = os.cpu_count()
    ALL_INPUTS = TOPO_PREDICTORS + HOMEMADE_PREDICTORS
    ALL_INPUTS += ['U_10M', 'V_10M'] if cosmoblurred else ERA5_PREDICTORS_Z500 + ERA5_PREDICTORS_SURFACE
    input_provider, output_provider = get_data_providers(data_provider=data_provider, cosmoblurred=cosmoblurred)
    run_id = f'{run_id}_cosmo_blurred' if cosmoblurred else run_id
    START_DATE = pd.to_datetime(start_date)
    END_DATE = pd.to_datetime(end_date)
    NUM_DAYS = (END_DATE - START_DATE).days + 1
    batch_gen = BatchGenerator(input_provider, output_provider,
                               decoder=NaiveDecoder(normalize=True),
                               sequence_length=sequence_length,
                               patch_length_pixel=img_size, batch_size=batch_size,
                               input_variables=ALL_INPUTS,
                               output_variables=ALL_OUTPUTS,
                               start_date=START_DATE, end_date=END_DATE,
                               num_workers=batch_workers)
    inputs = []
    outputs = []
    with batch_gen as batch:
        for b in range(NUM_DAYS):
            print(f'Creating batch {b + 1}/{NUM_DAYS}')
            x, y = next(batch)
            inputs.append(x)
            outputs.append(y)
    inputs = np.concatenate(inputs)
    outputs = np.concatenate(outputs)
    INPUT_CHANNELS = len(ALL_INPUTS)
    OUT_CHANNELS = len(ALL_OUTPUTS)

    # Plotting some results
    def show(images, dims=1, legends=None):
        fig, axes = plt.subplots(ncols=len(images), figsize=(10, 10))
        for ax, im in zip(axes, images):
            for i in range(dims):
                label = legends[i] if legends is not None else ''
                ax.imshow(im[0, :, :, i], cmap='jet')
                ax.set_title(label)
                ax.axis('off')
        return fig

    network = get_network_from_config(len_inputs=INPUT_CHANNELS, len_outputs=OUT_CHANNELS,
                                      sequence_length=sequence_length, img_size=img_size, batch_size=batch_size,
                                      noise_channels=noise_channels,
                                      gen_only=gen_only)
    str_net = 'generator' if gen_only else 'gan'
    gen = network if gen_only else network.generator
    # Saving results
    checkpoint_path_weights = Path(f'./checkpoints/{str_net}') / run_id / 'weights-{epoch:02d}.ckpt'

    for epoch in np.arange(saving_frequency, nb_epochs, saving_frequency):
        network.load_weights(str(checkpoint_path_weights).format(epoch=epoch))
        for i in range(0, batch_size * 6, batch_size):
            results = gen.predict(inputs[i:i + batch_size]) if gen_only else gen.predict(
                [inputs[i:i + batch_size],
                 network.noise_generator(bs=inputs[i:i + batch_size].shape[0], channels=noise_channels)])
            j = 0
            for inp, out, res in zip(inputs[i:i + batch_size], outputs[i:i + batch_size],
                                     results):
                plot_path = Path(
                    f'./plots/{str_net}') / run_id / str(epoch) / f'inp_{i}_batch_{j}.png'
                plot_path.parent.mkdir(exist_ok=True, parents=True)
                fig = show([inp, out, res])
                fig.savefig(plot_path)
                j += 1


def get_predicted_map_Switzerland(network, input_image, input_vars, img_size, sequence_size, gen_only=False,
                                  noise_channels=20, handle_borders='crop', average_preds=False):
    """

    :param network: the network model
    :param input_image: dataset (nc file) containing unprocessed inputs
    :param input_vars: selected input variables for model
    :param img_size: patch size as set up in model for the training
    :param sequence_size: time steps as set up in model for training
    :param noise_channels: noise channels as set up in model for training
    :param handle_borders: either 'crop' or 'overlap'
    :return:
    """
    variables_of_interest = ['u10', 'v10']
    pixels_lat, pixels_lon, time_window = input_image.dims['x_1'], input_image.dims['y_1'], input_image.dims['time']
    ntimeseq = time_window // sequence_size
    if handle_borders == 'crop':
        nrows, ncols = np.math.floor(pixels_lon / img_size), np.math.floor(pixels_lat / img_size)
        squares = {(i, j, k): input_image.isel(time=slice(k * sequence_size, (k + 1) * sequence_size),
                                               x_1=slice(j * img_size, (j + 1) * img_size),
                                               y_1=slice((ncols - i - 1) * img_size, (ncols - i - 2) * img_size, -1)
                                               )[input_vars]
                   for j in range(ncols)
                   for i in range(nrows)
                   for k in range(ntimeseq)}
    elif handle_borders == 'overlap':
        nrows, ncols = np.math.ceil(pixels_lon / img_size), np.math.ceil(
            pixels_lat / img_size)  # ceil and not floor, we want to cover the whole map
        xdist, ydist = (pixels_lat - img_size) // (ncols - 1), (pixels_lon - img_size) // (nrows - 1)
        leftovers_x, leftovers_y = pixels_lat - ((ncols - 1) * xdist + img_size), pixels_lon - (
                (nrows - 1) * ydist + img_size)
        x_vec_leftovers, y_vec_leftovers = np.concatenate(
            [[0], np.ones(leftovers_x), np.zeros(ncols - leftovers_x - 1)]).cumsum(), np.concatenate(
            [[0], np.ones(leftovers_y), np.zeros(nrows - leftovers_y - 1)]).cumsum()
        slices_start_x, slices_start_y = [int(i * xdist + x) for (i, x) in zip(range(ncols), x_vec_leftovers)], [
            int(j * ydist + y) for (j, y) in zip(range(nrows), y_vec_leftovers)]
        squares = {(sx, sy, k): input_image.isel(time=slice(k * sequence_size, (k + 1) * sequence_size),
                                                 x_1=slice(sx, sx + 128),
                                                 y_1=slice(sy + 127, sy - 1, -1) if sy != 0 else slice(128, 0, -1)
                                                 )[input_vars]
                   for sx in slices_start_x
                   for sy in slices_start_y
                   for k in range(ntimeseq)}
    else:
        raise ValueError('Please chose one of "crop" or "overlap" for handling of image borders')
    positions = {(i, j, k): index for index, (i, j, k) in enumerate(squares)}
    tensors = np.stack([im.to_array().to_numpy() for k, im in squares.items()], axis=0)
    tensors = np.transpose(tensors, [0, 2, 3, 4, 1])
    tensors = (tensors - np.nanmean(tensors, axis=(0, 1, 2), keepdims=True)) / np.nanstd(tensors, axis=(0, 1, 2),
                                                                                         keepdims=True)
    gen = network if gen_only else network.generator
    if average_preds:
        pred = []
        nb_sim = 50
        for _ in range(nb_sim):
            print(f'Computing prediction {_} over {nb_sim}')
            predictions = gen.predict(tensors) if gen_only else gen.predict(
                [tensors, network.noise_generator(bs=tensors.shape[0], channels=noise_channels)])
            pred.append(predictions)
        predictions = np.mean(np.stack(pred, axis=-1), axis=-1)
        std_pred = np.std(np.stack(pred, axis=-1), axis=-1)
    else:
        predictions = gen.predict(tensors) if gen_only else gen.predict(
            [tensors, network.noise_generator(bs=tensors.shape[0], channels=noise_channels)])
    predicted_squares = {
        (i, j, k): xr.Dataset(
            {v: xr.DataArray(predictions[positions[(i, j, k)], ..., variables_of_interest.index(v)],
                             coords=squares[(i, j, k)].coords, name=v) for v in
             variables_of_interest},
            coords=squares[(i, j, k)].coords)
        for (i, j, k) in squares
    }
    predicted_data = xr.combine_by_coords(list(predicted_squares.values()))
    if average_preds:
        predicted_std_squares = {
            (i, j, k): xr.Dataset(
                {v: xr.DataArray(std_pred[positions[(i, j, k)], ..., variables_of_interest.index(v)],
                                 coords=squares[(i, j, k)].coords, name=v) for v in
                 variables_of_interest},
                coords=squares[(i, j, k)].coords)
            for (i, j, k) in squares
        }
        predicted_std = xr.combine_by_coords(list(predicted_std_squares.values()))
    else:
        predicted_std = None
    return predicted_data, predicted_std


def save_predicted_maps_Switzerland():
    pass


def plot_predicted_maps_Swizterland(run_id, date, epoch, hour, variable_to_plot,
                                    sequence_length=6,
                                    img_size=128,
                                    batch_size=16,
                                    noise_channels=20,
                                    cosmoblurred=False,
                                    data_provider: str = 'local',
                                    gen_only=False,
                                    average_preds=False
                                    ):
    TOPO_PREDICTORS = ['tpi_500', 'slope', 'aspect']
    HOMEMADE_PREDICTORS = ['w_speed', 'w_angle']
    ERA5_PREDICTORS_SURFACE = ['u10', 'v10', 'blh', 'fsr', 'sp', 'sshf']
    ERA5_PREDICTORS_Z500 = ['z']
    ALL_OUTPUTS = ['U_10M', 'V_10M']
    ALL_INPUTS = []  # + TOPO_PREDICTORS + HOMEMADE_PREDICTORS
    ALL_INPUTS += ['U_10M', 'V_10M'] if cosmoblurred else ERA5_PREDICTORS_Z500 + ERA5_PREDICTORS_SURFACE
    input_provider, output_provider = get_data_providers(data_provider=data_provider, cosmoblurred=cosmoblurred)
    run_id = f'{run_id}_cosmo_blurred' if cosmoblurred else run_id
    network = get_network_from_config(len_inputs=len(ALL_INPUTS), len_outputs=len(ALL_OUTPUTS),
                                      sequence_length=sequence_length, img_size=img_size, batch_size=batch_size,
                                      noise_channels=noise_channels,
                                      gen_only=gen_only)
    str_net = 'generator' if gen_only else 'gan'
    # Saving results
    checkpoint_path_weights = Path(f'./checkpoints/{str_net}') / run_id / f'weights-{epoch}.ckpt'
    network.load_weights(str(checkpoint_path_weights))
    range_long = (5.73, 10.67)
    range_lat = (45.69, 47.97)
    with input_provider.provide(date) as input_file, output_provider.provide(date) as output_file:
        input_image = xr.open_dataset(input_file)
        df_in = input_image.to_dataframe()
        df_in = df_in[
            (df_in["lon_1"] <= range_long[1]) & (df_in["lon_1"] >= range_long[0]) & (df_in["lat_1"] <= range_lat[1]) & (
                    df_in["lat_1"] >= range_lat[0])]
        x_good = sorted(list(set(df_in.index.get_level_values("x_1"))))
        y_good = sorted(list(set(df_in.index.get_level_values("y_1"))))
        ds_in = input_image.sel(x_1=x_good, y_1=y_good)
        output_image = xr.open_dataset(output_file)
        ds_out = output_image.sel(x_1=x_good, y_1=y_good)
    predicted, std = get_predicted_map_Switzerland(network, ds_in, ALL_INPUTS, img_size=img_size,
                                                   sequence_size=sequence_length, noise_channels=noise_channels,
                                                   gen_only=gen_only, average_preds=average_preds)
    range_long = (5.8, 10.6)
    range_lat = (45.75, 47.9)
    fig = plt.figure(constrained_layout=False, figsize=(15, 5))
    gs = gridspec.GridSpec(1, 3, figure=fig) if not average_preds else gridspec.GridSpec(1, 4, figure=fig)
    ax1 = fig.add_subplot(gs[0, 0], projection=HigherResPlateCarree())
    ax2 = fig.add_subplot(gs[0, 1], projection=HigherResPlateCarree())
    ax3 = fig.add_subplot(gs[0, 2], projection=HigherResPlateCarree())
    axes = [ax1, ax2, ax3]
    cosmo_var = 'U_10M' if variable_to_plot == 'u10' else 'V_10M'
    text = 'U-component' if variable_to_plot == 'u10' else 'V-component'
    cbar_kwargs = {"orientation": "horizontal", "shrink": 0.7,
                   "label": f"10-meter {text} (m.s-1)"}
    inp = input_image.isel(time=hour).get(cosmo_var) if cosmoblurred else input_image.isel(time=hour).get(
        variable_to_plot)
    cosmo = output_image.isel(time=hour).get(cosmo_var)
    pred = predicted.isel(time=hour).get(variable_to_plot)
    std_var = std.isel(time=hour).get(variable_to_plot) if std is not None else None
    mini = np.nanmin(ds_out.isel(time=hour).get(cosmo_var).__array__())
    maxi = np.nanmax(ds_out.isel(time=hour).get(cosmo_var).__array__())
    vmin, vmax = -max(abs(mini), abs(maxi)), max(abs(mini), abs(maxi))
    inp.plot(cmap='jet', ax=ax1, transform=crs_cosmo, vmin=vmin, vmax=vmax,
             cbar_kwargs=cbar_kwargs)
    cosmo.plot(cmap='jet', ax=ax2, transform=crs_cosmo, vmin=vmin, vmax=vmax,
               cbar_kwargs=cbar_kwargs)
    pred.plot(cmap='jet', ax=ax3, transform=crs_cosmo, vmin=vmin, vmax=vmax,
              cbar_kwargs=cbar_kwargs)
    if average_preds:
        ax4 = fig.add_subplot(gs[0, 3], projection=HigherResPlateCarree())
        axes.append(ax4)
        std_var.plot(cmap='OrRd', ax=ax4, transform=crs_cosmo,
                     cbar_kwargs=cbar_kwargs)
        ax4.set_title('Predicted std')
        ax3.set_title('Predicted mean')
    else:
        ax3.set_title('Predicted')
    title_inp = 'COSMO1 blurred data' if cosmoblurred else 'ERA5 reanalysis data'
    ax1.set_title(title_inp)
    ax2.set_title('COSMO-1 data')
    for ax in axes:
        ax.set_extent([range_long[0], range_long[1], range_lat[0], range_lat[1]])
        ax.add_feature(cartopy.feature.BORDERS.with_scale('10m'), color='black')
    fig.tight_layout()
    return fig


def compute_metrics_val_set(run_id, start_date, end_date, sequence_length=6,
                            img_size=128,
                            batch_size=16,
                            noise_channels=20,
                            cosmoblurred=False,
                            batch_workers=None,
                            data_provider: str = 'local',
                            saving_frequency=10,
                            nb_epochs=500,
                            gen_only=False
                            ):
    TOPO_PREDICTORS = ['tpi_500', 'slope', 'aspect']
    HOMEMADE_PREDICTORS = ['w_speed', 'w_angle']
    ERA5_PREDICTORS_SURFACE = ['u10', 'v10', 'blh', 'fsr', 'sp', 'sshf']
    ERA5_PREDICTORS_Z500 = ['z']
    ALL_OUTPUTS = ['U_10M', 'V_10M']
    if batch_workers is None:
        batch_workers = os.cpu_count()
    ALL_INPUTS = [] + TOPO_PREDICTORS + HOMEMADE_PREDICTORS
    ALL_INPUTS += ['U_10M', 'V_10M'] if cosmoblurred else ERA5_PREDICTORS_Z500 + ERA5_PREDICTORS_SURFACE
    input_provider, output_provider = get_data_providers(data_provider=data_provider, cosmoblurred=cosmoblurred)
    run_id = f'{run_id}_cosmo_blurred' if cosmoblurred else run_id
    START_DATE = pd.to_datetime(start_date)
    END_DATE = pd.to_datetime(end_date)
    NUM_DAYS = (END_DATE - START_DATE).days + 1
    batch_gen = BatchGenerator(input_provider, output_provider,
                               decoder=NaiveDecoder(normalize=True),
                               sequence_length=sequence_length,
                               patch_length_pixel=img_size, batch_size=batch_size,
                               input_variables=ALL_INPUTS,
                               output_variables=ALL_OUTPUTS,
                               start_date=START_DATE, end_date=END_DATE,
                               num_workers=batch_workers)
    inputs = []
    outputs = []
    with batch_gen as batch:
        for b in range(NUM_DAYS):
            print(f'Creating batch {b + 1}/{NUM_DAYS}')
            x, y = next(batch)
            inputs.append(x)
            outputs.append(y)
    inputs = np.concatenate(inputs)
    outputs = np.concatenate(outputs)
    INPUT_CHANNELS = len(ALL_INPUTS)
    OUT_CHANNELS = len(ALL_OUTPUTS)

    network = get_network_from_config(len_inputs=INPUT_CHANNELS, len_outputs=OUT_CHANNELS,
                                      sequence_length=sequence_length, img_size=img_size, batch_size=batch_size,
                                      noise_channels=noise_channels,
                                      gen_only=gen_only)
    str_net = 'generator' if gen_only else 'gan'
    gen = network if gen_only else network.generator
    # Saving results
    checkpoint_path_weights = Path(f'./checkpoints/{str_net}') / run_id / 'weights-{epoch:02d}.ckpt'
    metrics_data = []
    for epoch in np.arange(saving_frequency, nb_epochs, saving_frequency):
        print(f'Loading weights for epoch {epoch}')
        network.load_weights(str(checkpoint_path_weights).format(epoch=epoch))
        preds = gen.predict(inputs) if gen_only else gen.predict(
            [inputs, network.noise_generator(bs=inputs.shape[0], channels=noise_channels)])
        print(f'Computing metrics for epoch {epoch}')
        metrics_data.append(
            pd.DataFrame([float(m()(outputs, preds)) for m in TEST_METRICS], index=[m().name for m in TEST_METRICS],
                         columns=[epoch]).T)
    metrics_data = pd.concat(metrics_data)
    return metrics_data


def plot_metrics_data(metrics_data_csv):
    """

    :param metrics_data_csv: data is computed using the above function
    :return:
    """
    metrics_data_csv = metrics_data_csv.rename(columns={"ws_rmse": "Wind Speed RMSE",
                                                        "ws_weighted_rmse": "Wind Speed Weighted RMSE",
                                                        "extreme_rmse": "Extreme RMSE",
                                                        "spatial_ks": "Spatially Convolved KS Statistics",
                                                        "acd": "Angular Cosine Distance",
                                                        "lsd": "Log Spectral Distance"})
    if 'Unnamed: 0' in metrics_data_csv.columns:
        metrics_data_csv = metrics_data_csv.rename(columns={"Unnamed: 0": "epoch"}).set_index("epoch")
    fig, axes = plt.subplots(2, 3, figsize=(20, 10))
    axes = axes.flatten()
    for c, ax in zip(metrics_data_csv.columns, axes):
        df = metrics_data_csv[c]
        title = c
        three_min = df.sort_values().iloc[:3]
        ax.plot(df, color="royalblue", ls='dotted')
        ax.scatter(three_min.index, three_min, color="navy", marker='^')
        ax.set_xlabel("Epoch")
        ax.set_title(title)
    fig.tight_layout()
    return fig


