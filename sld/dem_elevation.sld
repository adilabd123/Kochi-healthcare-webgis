<?xml version="1.0" encoding="UTF-8"?>
<StyledLayerDescriptor version="1.0.0"
    xmlns="http://www.opengis.net/sld"
    xmlns:ogc="http://www.opengis.net/ogc"
    xmlns:xlink="http://www.w3.org/1999/xlink"
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
    xsi:schemaLocation="http://www.opengis.net/sld http://schemas.opengis.net/sld/1.0.0/StyledLayerDescriptor.xsd">
  <NamedLayer>
    <Name>dem_elevation</Name>
    <UserStyle>
      <Name>dem_elevation</Name>
      <Title>Copernicus GLO-30 elevation (m, EGM2008)</Title>
      <Abstract>Ramp from the 2nd (1.0 m) to the 98th (13.92 m) percentile of analysis/kochi_dem.tif</Abstract>
      <FeatureTypeStyle>
        <Rule>
          <Name>elevation</Name>
          <RasterSymbolizer>
            <Opacity>1.0</Opacity>
            <ColorMap type="ramp">
              <ColorMapEntry color="#000000" quantity="-9999" opacity="0.0"/>
              <ColorMapEntry color="#1a9850" quantity="1.0" label="1 m" opacity="0.75"/>
              <ColorMapEntry color="#91cf60" quantity="3.6" label="4 m" opacity="0.75"/>
              <ColorMapEntry color="#d9ef8b" quantity="6.2" label="6 m" opacity="0.75"/>
              <ColorMapEntry color="#fee08b" quantity="8.8" label="9 m" opacity="0.75"/>
              <ColorMapEntry color="#fc8d59" quantity="11.4" label="11 m" opacity="0.75"/>
              <ColorMapEntry color="#d73027" quantity="14.0" label="14 m" opacity="0.75"/>
            </ColorMap>
          </RasterSymbolizer>
        </Rule>
      </FeatureTypeStyle>
    </UserStyle>
  </NamedLayer>
</StyledLayerDescriptor>
