import torch
from omegaconf import OmegaConf
from transformers import CLIPTextModel, CLIPTokenizer
from diffusers import AutoencoderKL, LMSDiscreteScheduler
from my_model import unet_2d_condition
import json
from PIL import Image
from utils import compute_ca_loss, Pharse2idx, draw_box, setup_logger
import hydra
import os
from tqdm import tqdm
from utils import load_text_inversion


def inference(device, unet, vae, tokenizer, text_encoder, prompt, bboxes, phrases, cfg, logger):


    logger.info("Inference")
    logger.info(f"Prompt: {prompt}")
    logger.info(f"Phrases: {phrases}")

    # Get Object Positions

    logger.info("Convert Phrases to Object Positions")
    object_positions = Pharse2idx(prompt, phrases)

    # Encode Classifier Embeddings
    uncond_input = tokenizer(
        [""] * cfg.inference.batch_size, padding="max_length", max_length=tokenizer.model_max_length, return_tensors="pt"
    )
    uncond_embeddings = text_encoder(uncond_input.input_ids.to(device))[0]

    # Encode Prompt
    input_ids = tokenizer(
            [prompt] * cfg.inference.batch_size,
            padding="max_length",
            truncation=True,
            max_length=tokenizer.model_max_length,
            return_tensors="pt",
        )

    cond_embeddings = text_encoder(input_ids.input_ids.to(device))[0]
    text_embeddings = torch.cat([uncond_embeddings, cond_embeddings])
    generator = torch.manual_seed(cfg.inference.rand_seed)  # Seed generator to create the initial latent noise

    noise_scheduler = LMSDiscreteScheduler(beta_start=cfg.noise_schedule.beta_start, beta_end=cfg.noise_schedule.beta_end,
                                           beta_schedule=cfg.noise_schedule.beta_schedule, num_train_timesteps=cfg.noise_schedule.num_train_timesteps)

    latents = torch.randn(
        (cfg.inference.batch_size, 4, 64, 64),
        generator=generator,
    ).to(device)

    noise_scheduler.set_timesteps(cfg.inference.timesteps)

    latents = latents * noise_scheduler.init_noise_sigma

    loss = torch.tensor(10000)

    for index, t in enumerate(tqdm(noise_scheduler.timesteps)):
        iteration = 0

        while loss.item() / cfg.inference.loss_scale > cfg.inference.loss_threshold and iteration < cfg.inference.max_iter and index < cfg.inference.max_index_step:
            latents = latents.requires_grad_(True)
            latent_model_input = latents
            latent_model_input = noise_scheduler.scale_model_input(latent_model_input, t)
            noise_pred, attn_map_integrated_up, attn_map_integrated_mid, attn_map_integrated_down = \
                unet(latent_model_input, t, encoder_hidden_states=cond_embeddings)

            # update latents with guidance
            loss = compute_ca_loss(attn_map_integrated_mid, attn_map_integrated_up, bboxes=bboxes,
                                   object_positions=object_positions) * cfg.inference.loss_scale

            grad_cond = torch.autograd.grad(loss.requires_grad_(True), [latents])[0]

            latents = latents - grad_cond * noise_scheduler.sigmas[index] ** 2
            iteration += 1
            torch.cuda.empty_cache()

        with torch.no_grad():
            latent_model_input = torch.cat([latents] * 2)

            latent_model_input = noise_scheduler.scale_model_input(latent_model_input, t)
            noise_pred, attn_map_integrated_up, attn_map_integrated_mid, attn_map_integrated_down = \
                unet(latent_model_input, t, encoder_hidden_states=text_embeddings)

            noise_pred = noise_pred.sample

            # perform guidance
            noise_pred_uncond, noise_pred_text = noise_pred.chunk(2)
            noise_pred = noise_pred_uncond + cfg.inference.classifier_free_guidance * (noise_pred_text - noise_pred_uncond)

            latents = noise_scheduler.step(noise_pred, t, latents).prev_sample
            torch.cuda.empty_cache()

    with torch.no_grad():
        logger.info("Decode Image...")
        latents = 1 / 0.18215 * latents
        image = vae.decode(latents).sample
        image = (image / 2 + 0.5).clamp(0, 1)
        image = image.detach().cpu().permute(0, 2, 3, 1).numpy()
        images = (image * 255).round().astype("uint8")
        pil_images = [Image.fromarray(image) for image in images]
        return pil_images


@hydra.main(version_base=None, config_path="conf", config_name="base_config")
def main(cfg):

    # build and load model
    with open(cfg.general.unet_config) as f:
        unet_config = json.load(f)
    unet = unet_2d_condition.UNet2DConditionModel(**unet_config).from_pretrained(cfg.general.model_path, subfolder="unet")
    tokenizer = CLIPTokenizer.from_pretrained(cfg.general.model_path, subfolder="tokenizer")
    text_encoder = CLIPTextModel.from_pretrained(cfg.general.model_path, subfolder="text_encoder")
    vae = AutoencoderKL.from_pretrained(cfg.general.model_path, subfolder="vae")

    if cfg.general.real_image_editing:
        text_encoder, tokenizer = load_text_inversion(text_encoder, tokenizer, cfg.real_image_editing.placeholder_token, cfg.real_image_editing.text_inversion_path)
        unet.load_state_dict(torch.load(cfg.real_image_editing.dreambooth_path)['unet'])
        text_encoder.load_state_dict(torch.load(cfg.real_image_editing.dreambooth_path)['encoder'])

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    unet.to(device)
    text_encoder.to(device)
    vae.to(device)



    # ------------------ example input ------------------
    examples = {"prompt": "A hello kitty toy is playing with a purple ball.",
                "phrases": "hello kitty; ball",
                "bboxes": [[[0.1, 0.2, 0.5, 0.8]], [[0.75, 0.6, 0.95, 0.8]]],
                'save_path': cfg.general.save_path
                }

    examples = {"prompt": "A rabbit wearing sunglasses looks very proud",
                "phrases": "rabbit; sunglasses",
                "bboxes": [[[0.130859375, 0.169921875, 0.71484375, 1.0]], [[0.12890625, 0.25390625, 0.7109375, 0.51171875]]],
                'save_path': cfg.general.save_path
                }

    # ------------------ real image editing example input ------------------
    if cfg.general.real_image_editing:
        examples = {"prompt": "A {} is standing on grass.".format(cfg.real_image_editing.placeholder_token),
                    "phrases": "{}".format(cfg.real_image_editing.placeholder_token),
                    "bboxes": [[[0.4, 0.2, 0.9, 0.9]]],
                    'save_path': cfg.general.save_path
                    }
    # ---------------------------------------------------
    # Prepare the save path
    if not os.path.exists(cfg.general.save_path):
        os.makedirs(cfg.general.save_path)
    logger = setup_logger(cfg.general.save_path, __name__)

    logger.info(cfg)
    # Save cfg
    logger.info("save config to {}".format(os.path.join(cfg.general.save_path, 'config.yaml')))
    OmegaConf.save(cfg, os.path.join(cfg.general.save_path, 'config.yaml'))

    # Inference
    pil_images = inference(device, unet, vae, tokenizer, text_encoder, examples['prompt'], examples['bboxes'], examples['phrases'], cfg, logger)

    # Save example images
    for index, pil_image in enumerate(pil_images):
        image_path = os.path.join(cfg.general.save_path, 'example_{}.png'.format(index))
        logger.info('save example image to {}'.format(image_path))
        draw_box(pil_image, examples['bboxes'], examples['phrases'], image_path)

if __name__ == "__main__":
    main()