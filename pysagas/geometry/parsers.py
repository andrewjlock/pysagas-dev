from stl import mesh
from tqdm import tqdm
from typing import List
import multiprocess as mp
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from pysagas.geometry import Cell, Vector


class AbstractParser(ABC):
    """Interface for a geometry parser."""

    filetype = None

    @abstractmethod
    def __init__(self, **kwargs) -> None:
        pass

    def __repr__(self) -> str:
        return f"PySAGAS {self.filetype} parser"

    def __str__(self) -> str:
        return f"PySAGAS {self.filetype} parser"

    @property
    @abstractmethod
    def filetype(self):
        # This is a placeholder for a class variable defining the parser file type
        pass

    @classmethod
    @abstractmethod
    def load_from_file(self) -> List[Cell]:
        """Convenience method for loading cells from file."""


class Parser(AbstractParser):
    def __init__(self, filepath: str, verbosity: int = 1) -> None:
        self.filepath = filepath
        self.verbosity = verbosity


class STL(Parser):
    filetype = "STL"

    def load(self) -> List[Cell]:
        # Load the STL
        mesh_obj = mesh.Mesh.from_file(self.filepath)

        cells = []
        # TODO - can face ids be inferred?
        if self.verbosity > 0:
            print("Transcribing cells:")
            pbar = tqdm(
                total=len(mesh_obj.vectors),
                position=0,
                leave=True,
                desc="  Cell transcription progress",
            )

        for vector_triple in mesh_obj.vectors:
            vertices = [Vector.from_coordinates(v) for v in vector_triple]
            try:
                cell = Cell.from_points(vertices)
                cells.append(cell)
            except:
                pass

            # Update progress bar
            if self.verbosity > 0:
                pbar.update(1)

        if self.verbosity > 0:
            pbar.close()
            print("Done.")

        return cells

    @classmethod
    def load_from_file(cls, filepath: str, verbosity: int = 1) -> List[Cell]:
        """Convenience method for loading cells from file."""
        # Create parser instance
        parser = cls(filepath, verbosity)

        # Load file
        cells = parser.load()

        return cells


class PyMesh(Parser):
    filetype = "PyMesh STL"

    def __init__(self, filepath: str, verbosity: int = 1) -> None:
        # Import PyMesh
        try:
            import pymesh
        except ModuleNotFoundError:
            raise Exception(
                "Could not find pymesh. Please follow the "
                + "installation instructions at "
                + "https://pymesh.readthedocs.io/en/latest/installation.html"
            )
        self._pymesh = pymesh
        super().__init__(filepath, verbosity)

    def load(self) -> List[Cell]:
        def mp_wrapper(face):
            vertices = [Vector.from_coordinates(mesh_vertices[i]) for i in face]
            try:
                cell = Cell.from_points(vertices, face_ids=face)
            except:
                cell = None
            return cell

        # Load the STL
        mesh_obj = self._pymesh.load_mesh(self.filepath)

        if self.verbosity > 0:
            print("Transcribing cells.")

        # Create multiprocessing pool to construct cells
        cells = []
        pool = mp.Pool()
        mesh_vertices = mesh_obj.vertices
        for result in pool.map(mp_wrapper, mesh_obj.faces):
            if result is not None:
                cells.append(result)

        if self.verbosity > 0:
            print("Done.")

        return cells

    @classmethod
    def load_from_file(cls, filepath: str, verbosity: int = 1) -> List[Cell]:
        """Convenience method for loading cells from file."""
        # Create parser instance
        parser = cls(filepath, verbosity)

        # Load file
        cells = parser.load()

        return cells


class TRI(Parser):
    filetype = ".tri"

    def load(self) -> List[Cell]:
        # Parse .tri file
        tree = ET.parse(self.filepath)
        root = tree.getroot()
        grid = root[0]
        piece = grid[0]
        points = piece[0]
        cells = piece[1]

        points_data = points[0].text
        cells_data = cells[0].text

        points_data_list = [el.split() for el in points_data.splitlines()[1:]]
        points_data_list = [[float(j) for j in i] for i in points_data_list]

        cells_data_list = [el.split() for el in cells_data.splitlines()[1:]]
        cells_data_list = [[int(j) for j in i] for i in cells_data_list]

        cells = []
        if self.verbosity > 0:
            print("Transcribing cells:")
            pbar = tqdm(
                total=len(cells_data_list),
                position=0,
                leave=True,
                desc="  Cell transcription progress",
            )
        for vertex_idxs in cells_data_list:
            vertices = [
                Vector.from_coordinates(points_data_list[i]) for i in vertex_idxs
            ]
            cell = Cell.from_points(vertices, face_ids=vertex_idxs)
            cells.append(cell)

            # Update progress bar
            if self.verbosity > 0:
                pbar.update(1)

        if self.verbosity > 0:
            pbar.close()
            print("Done.")

        return cells

    @classmethod
    def load_from_file(cls, filepath: str, verbosity: int = 1) -> List[Cell]:
        """Convenience method for loading cells from file."""
        # Create parser instance
        parser = cls(filepath, verbosity)

        # Load file
        cells = parser.load()

        return cells