<?xml version="1.0" encoding="UTF-8"?>
<StyledLayerDescriptor version="1.0.0"
    xmlns="http://www.opengis.net/sld"
    xmlns:ogc="http://www.opengis.net/ogc"
    xmlns:xlink="http://www.w3.org/1999/xlink"
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
    xsi:schemaLocation="http://www.opengis.net/sld http://schemas.opengis.net/sld/1.0.0/StyledLayerDescriptor.xsd">
  <NamedLayer>
    <Name>hospital_access_gaps</Name>
    <UserStyle>
      <Name>hospital_access_gaps</Name>
      <Title>Elderly hospital-access gaps</Title>
      <Abstract>Buildings more than 15 min (3.6 km/h) from a hospital-tagged facility</Abstract>
      <FeatureTypeStyle>
        <Rule>
          <Name>Gap at 4.8 and 3.6 km/h</Name>
          <Title>Gap at 4.8 and 3.6 km/h</Title>
          <ogc:Filter><ogc:PropertyIsEqualTo><ogc:PropertyName>gap_class</ogc:PropertyName><ogc:Literal>gap_at_4.8_and_3.6</ogc:Literal></ogc:PropertyIsEqualTo></ogc:Filter>
          <PolygonSymbolizer>
            <Fill><CssParameter name="fill">#7f1d1d</CssParameter><CssParameter name="fill-opacity">0.9</CssParameter></Fill>
            <Stroke><CssParameter name="stroke">#450a0a</CssParameter><CssParameter name="stroke-width">0.3</CssParameter></Stroke>
          </PolygonSymbolizer>
        </Rule>
        <Rule>
          <Name>Gap at 3.6 km/h only</Name>
          <Title>Gap at 3.6 km/h only</Title>
          <ogc:Filter><ogc:PropertyIsEqualTo><ogc:PropertyName>gap_class</ogc:PropertyName><ogc:Literal>gap_at_3.6_only</ogc:Literal></ogc:PropertyIsEqualTo></ogc:Filter>
          <PolygonSymbolizer>
            <Fill><CssParameter name="fill">#f97316</CssParameter><CssParameter name="fill-opacity">0.85</CssParameter></Fill>
            <Stroke><CssParameter name="stroke">#7c2d12</CssParameter><CssParameter name="stroke-width">0.3</CssParameter></Stroke>
          </PolygonSymbolizer>
        </Rule>
      </FeatureTypeStyle>
    </UserStyle>
  </NamedLayer>
</StyledLayerDescriptor>
