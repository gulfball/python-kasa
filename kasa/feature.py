"""Generic interface for defining device features."""
from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING, Any, Callable, Optional, Union

if TYPE_CHECKING:
    from .device import Device


class FeatureType(Enum):
    """Type to help decide how to present the feature."""

    Sensor = auto()
    BinarySensor = auto()
    Switch = auto()
    Button = auto()


@dataclass
class Feature:
    """Feature defines a generic interface for device features."""

    #: Device instance required for getting and setting values
    device: "Device"
    #: User-friendly short description
    name: str
    #: Name of the property that allows accessing the value
    attribute_getter: Union[str, Callable]
    #: Name of the method that allows changing the value
    attribute_setter: Optional[str] = None
    #: Container storing the data, this overrides 'device' for getters
    container: Any = None
    #: Icon suggestion
    icon: Optional[str] = None
    #: Type of the feature
    type: FeatureType = FeatureType.Sensor

    @property
    def value(self):
        """Return the current value."""
        container = self.container if self.container is not None else self.device
        if isinstance(self.attribute_getter, Callable):
            return self.attribute_getter(container)
        return getattr(container, self.attribute_getter)

    async def set_value(self, value):
        """Set the value."""
        if self.attribute_setter is None:
            raise ValueError("Tried to set read-only feature.")
        container = self.container if self.container is not None else self.device
        return await getattr(container, self.attribute_setter)(value)
