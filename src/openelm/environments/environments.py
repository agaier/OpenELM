import math
import string
from abc import ABC, abstractmethod
from dataclasses import is_dataclass
from typing import Generic, Optional, TypeVar, Union

import numpy as np
from omegaconf import DictConfig, OmegaConf

from openelm.configs import BaseConfig, ImageELMConfig, SodaraceELMConfig
from openelm.diff_model import PromptMutationForImgTask, PromptMutationForSodarace
from openelm.environments.sodaracer import SodaraceSimulator

Phenotype = Optional[np.ndarray]


def ackley(x: np.ndarray) -> np.ndarray:
    d = x.shape[-1]
    a = 5
    b = 0.1

    o1 = -a * np.exp(-b * np.sqrt(np.sum(x**2, axis=1) / d))
    o2 = -np.exp(np.sum(np.cos(math.tau * x) / d, axis=1))

    return -(a + math.exp(1) + o1 + o2)


class Genotype(ABC):
    def __str__(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def to_phenotype(self) -> Optional[Phenotype]:
        raise NotImplementedError


GenoType = TypeVar("GenoType", bound=Genotype)


class BaseEnvironment(ABC, Generic[GenoType]):
    def __init__(self) -> None:
        self.genotype_space: np.ndarray
        self.batch_size: int

    @abstractmethod
    def random(self) -> list[GenoType]:
        raise NotImplementedError

    @abstractmethod
    def mutate(self, x: list[GenoType]) -> list[GenoType]:
        raise NotImplementedError

    @abstractmethod
    def fitness(self, x: GenoType) -> float:
        raise NotImplementedError

    @property
    def max_fitness(self) -> int:
        return 0

    @property
    # [starts, endings) of search intervals
    def behavior_space(self) -> np.ndarray:
        return self.genotype_space

    @property
    def behavior_ndim(self) -> int:
        return self.behavior_space.shape[1]

    @staticmethod
    def _load_config(config):
        # TODO: convert all to dataclass
        if isinstance(config, str):
            return OmegaConf.load(config)
        elif isinstance(config, (dict, DictConfig)):
            return DictConfig(config)
        elif is_dataclass(config):
            return OmegaConf.structured(config)
        else:
            raise ValueError


class ArrayGenotype(Genotype, np.ndarray):
    def __new__(cls, input_array):
        obj = np.asarray(input_array).view(cls)
        return obj

    def __str__(self) -> str:
        return f'({", ".join(map(str, np.asarray(self)))})'

    def to_phenotype(self) -> Phenotype:
        return np.asarray(self)


# find all local maxima of a multimodal function
class FunctionOptim(BaseEnvironment[ArrayGenotype]):
    def __init__(self, ndim=2):
        self.genotype_ndim = ndim
        self.genotype_space = np.repeat([[-4, 4]], self.genotype_ndim, axis=0).T
        self.batch_size: int = 1

    def random(self) -> list[ArrayGenotype]:
        return [
            ArrayGenotype(np.random.uniform(*self.genotype_space))
            for _ in range(self.batch_size)
        ]

    def mutate(self, x: list[ArrayGenotype]) -> list[ArrayGenotype]:
        for i in range(self.batch_size):
            ix = np.random.randint(self.genotype_ndim)
            x[i][ix] = x[i][ix] + np.random.uniform(-1, 1)
        return x

    def fitness(self, x: ArrayGenotype) -> float:
        return ackley(x[None])[0]


class StringArrayGenotype(ArrayGenotype):
    def __str__(self) -> str:
        x: np.ndarray = np.round(self)
        return "".join(
            string.ascii_letters[ix]
            for ix in np.clip(x.astype(int), 0, len(string.ascii_letters) - 1)
        )

    def to_phenotype(self) -> Phenotype:
        return np.asarray(self)


class MatchString(BaseEnvironment[StringArrayGenotype]):
    # find a string by mutating one character at a time

    def __init__(self, target: str):
        self.alphabet = string.ascii_letters

        self.target = np.array([self.alphabet.index(ch) for ch in target])
        self.genotype_ndim = self.target.shape[0]
        self.genotype_space = np.repeat(
            [[0, len(self.alphabet)]], self.genotype_ndim, axis=0
        ).T

    def random(self) -> list[StringArrayGenotype]:
        return [
            StringArrayGenotype(np.random.uniform(*self.genotype_space))
            for _ in range(self.batch_size)
        ]

    def mutate(self, x: list[StringArrayGenotype]) -> list[StringArrayGenotype]:
        for i in range(self.batch_size):
            ix = np.random.randint(self.genotype_ndim)
            x[i][ix] = x[i][ix] + np.random.uniform(-1, 1)
        return x

    def fitness(self, x: StringArrayGenotype) -> float:
        return -np.abs(x - self.target).sum()


class ImageGeneration(Genotype):
    """Genotype for generated images."""

    def __init__(self, program_str: str, result_obj: np.ndarray):
        self.program_str = program_str
        self.result_obj = result_obj
        self.valid = self.validate()

    def __str__(self) -> str:
        if self.valid:
            return str(self.result_obj.reshape((-1, 3)).mean(axis=0).astype(int))
        else:
            return ""

    def validate(self) -> bool:
        return len(self.result_obj.shape) == 3 and self.result_obj.shape[2] == 3

    def to_phenotype(self, mode: str = "3-channel-avg") -> Optional[Phenotype]:
        if not self.valid:
            return None
        if mode == "3-channel-avg":
            # Average RGB channels.
            # Assume the input is of shape (height, width, channel), and we
            # average each channel to get (channel,)
            return np.average(self.result_obj.reshape((-1, 3)), axis=0)
        else:
            return None


class ImageOptim(BaseEnvironment[ImageGeneration]):
    """
    Mutate programs that return images.

    Fitness is simply the absolute difference between the returning
    image and the target image. To map into the behavior space,
    if behavior_mode=="3-channel", the image will be divided into blocks
    (specified in `block_size`), and average
    values of RGB channels in each block will be put together as a point in the
    behavior space (average-pooling).
    """

    default_diff_model_cls = PromptMutationForImgTask
    # Record different definitions of behavior spaces in a dict. Feel free to add.
    behavior_mode_spec = {"3-channel-avg": {"genotype_ndim": 3}}

    def __init__(
        self,
        seed: dict,
        config: Union[str, dict, DictConfig],
        target_img: np.ndarray,
        diff_model,
        behavior_mode: str = "3-channel",
        run_name: Optional[str] = None,
    ):
        """
        Mutate programs that return images.

        Fitness is simply the absolute difference between the returning
        image and the target image. To map into the behavior space,
        if behavior_mode=="3-channel", the image will be divided into blocks
        (specified in `block_size`), and average values of RGB channels in each
        block will be put together as a point in the behavior space (average-pooling).

        Args:
            seed: the seed dict.
            config: the config file path or dict.
            target_img: the target image.
            diff_model: the diff model (or alternatives).
            behavior_mode: (Optional) a string indicating the way an individual
            is mapped into behavior space.
            run_name: (Optional) override the run_name in config.
        """
        if isinstance(seed, dict):
            self.seed = ImageGeneration(**seed)
        else:
            raise TypeError

        self.config: ImageELMConfig = self._load_config(config)
        if run_name is not None:
            self.config.run_name = run_name

        self.target_img = target_img
        self.shape = target_img.shape

        if diff_model is None:
            self.diff_model = self.default_diff_model_cls(self.config)
        else:
            self.diff_model = diff_model

        self.behavior_mode = behavior_mode
        self.genotype_ndim: int = self.behavior_mode_spec[self.behavior_mode][
            "genotype_ndim"
        ]
        self.genotype_space = np.repeat([[0, 255]], self.genotype_ndim, axis=0).T

    def generate_program(self, code_batch: list[str]) -> list[ImageGeneration]:
        """
        Call LM to generate a new program and run it.

        Returns:
            An ImageGeneration object containing the code, the resulting image
            and the error code.
        """
        generated_programs = self.diff_model.generate_program(code_batch)
        return [ImageGeneration(**p) for p in generated_programs]

    def random(self) -> list[ImageGeneration]:
        """
        Randomly generate a batch of codes and evaluate their outputs.

        Returns:
            a tuple of the code string and the returning result (None if there
            is error).
        """
        program_str_list = [self.seed.program_str] * self.batch_size
        new_images = self.generate_program(program_str_list)
        return new_images

    def mutate(self, images_list: list[ImageGeneration]) -> list[ImageGeneration]:
        """
        Randomly mutate a batch of codes and evaluate their outputs.

        Args:
            x: the individual to be mutated.

        Returns:
            a tuple of the code string and the returning result (None if there
            is an error).
        """
        program_str_list = [sr.program_str for sr in images_list]
        new_images = self.generate_program(program_str_list)
        return new_images

    def fitness(self, x: ImageGeneration) -> float:
        if not x.valid or x.result_obj.shape != self.shape:
            return -np.inf
        return -np.abs(x.result_obj - self.target_img).sum()


class Sodaracer(Genotype):
    def __init__(self, program_str: str, result_obj: dict):
        """
        The Sodaracer genotype.

        Args:
            program_str: the string for the original code.
            result_obj: the dict of sodaracer.
        """
        self.program_str: str = program_str
        self.result_obj: dict = result_obj
        # self._fitness: Optional[float] = None

        # Check whether the Sodaracer is valid.
        try:
            # Test the Sodaracer by instantiating a simulation.
            self.simulator = SodaraceSimulator(body=self.result_obj)
            self.morphology = self.simulator.morphology
            self.evaluate(0)
            self.valid = True
        except Exception:
            self.valid = False

    def evaluate(self, eval_ms: int) -> float:
        self._fitness = self.simulator.evaluate(eval_ms)
        # if self._fitness is None:
        return self._fitness

    def __str__(self) -> str:
        return self.program_str

    def to_phenotype(self) -> Optional[Phenotype]:
        if self.valid:
            return np.array(
                [
                    self.morphology["height"],
                    self.morphology["width"],
                    self.morphology["mass"],
                ]
            ).astype(int)
        else:
            return None

    @property
    def fitness(self) -> Optional[float]:
        return self._fitness


class Sodarace(BaseEnvironment[Sodaracer]):
    default_diff_model_cls = PromptMutationForSodarace

    def __init__(
        self,
        seed: dict,
        config: Union[str, dict, DictConfig, BaseConfig],
        diff_model,
        eval_ms: int,
        max_height: int = 1000,
        max_width: int = 1000,
        max_mass: int = 2000,
        ndim: int = 3,
        run_name: Optional[str] = None,
    ) -> None:
        """
        Sodarace environment.

        Args:
            seed: the seed dict.
            config: the config file path or dict.
            diff_model: the diff model (or alternatives).
            eval_ms: The time in ms for sodaracer evaluation.
            max_height: (Optional) the maximal height.
            max_width: (Optional) the maximal width.
            max_mass: (Optional) the maximal mass.
            ndim: (Optional) the dimension of behavior space.
            run_name: (Optional) override the run_name in config.
        """
        if isinstance(seed, dict):
            self.seed = Sodaracer(**seed)
        else:
            raise TypeError
        # TODO: rewrite config code to make everything an instance of a dataclass
        self.config: SodaraceELMConfig = self._load_config(config)
        if run_name is not None:
            self.config.run_name = run_name

        if diff_model is None:
            self.diff_model = self.default_diff_model_cls(self.config)
        else:
            self.diff_model = diff_model

        self.batch_size = self.config.batch_size
        self.eval_ms = eval_ms
        self.genotype_ndim = ndim
        self.genotype_space = np.array(
            [[0, max_height], [0, max_width], [0, max_mass]]
        ).T

    def generate_program(self, code_batch: list[str]) -> list[Sodaracer]:
        # Call LM to generate a new program and run it, returning a dict
        # containing the program string and the dict from running it.
        generated_programs = self.diff_model.generate_program(code_batch)
        return [Sodaracer(**p) for p in generated_programs]

    def random(self) -> list[Sodaracer]:
        program_str_list = [self.seed.program_str] * self.batch_size
        new_sodaracers = self.generate_program(program_str_list)
        return new_sodaracers

    def mutate(self, sodaracer_list: list[Sodaracer]) -> list[Sodaracer]:
        program_str_list = [sr.program_str for sr in sodaracer_list]
        new_sodaracers = self.generate_program(program_str_list)
        return new_sodaracers

    def fitness(self, x: Sodaracer) -> float:
        # Call Sodaracers environment to get the fitness.
        if x.valid:
            return x.evaluate(self.eval_ms)
        else:
            return -np.inf
