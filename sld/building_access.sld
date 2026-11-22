<?xml version="1.0" encoding="UTF-8"?>
<StyledLayerDescriptor version="1.0.0"
    xmlns="http://www.opengis.net/sld"
    xmlns:ogc="http://www.opengis.net/ogc"
    xmlns:xlink="http://www.w3.org/1999/xlink"
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
    xsi:schemaLocation="http://www.opengis.net/sld http://schemas.opengis.net/sld/1.0.0/StyledLayerDescriptor.xsd">
  <NamedLayer>
    <Name>building_access</Name>
    <UserStyle>
      <Name>building_access</Name>
      <Title>Buildings by walking time to nearest healthcare (4.8 km/h)</Title>
      <Abstract>Classified on all_band4p8 from phase2_network.py</Abstract>
      <FeatureTypeStyle>
        <Rule>
          <Name>0-5 min</Name>
          <Title>0-5 min</Title>
          <ogc:Filter><ogc:PropertyIsEqualTo><ogc:PropertyName>all_band4p8</ogc:PropertyName><ogc:Literal>0-5 min</ogc:Literal></ogc:PropertyIsEqualTo></ogc:Filter>
          <PolygonSymbolizer>
            <Fill><CssParameter name="fill">#15803d</CssParameter><CssParameter name="fill-opacity">0.85</CssParameter></Fill>
            <Stroke><CssParameter name="stroke">#374151</CssParameter><CssParameter name="stroke-width">0.2</CssParameter></Stroke>
          </PolygonSymbolizer>
        </Rule>
        <Rule>
          <Name>5-10 min</Name>
          <Title>5-10 min</Title>
          <ogc:Filter><ogc:PropertyIsEqualTo><ogc:PropertyName>all_band4p8</ogc:PropertyName><ogc:Literal>5-10 min</ogc:Literal></ogc:PropertyIsEqualTo></ogc:Filter>
          <PolygonSymbolizer>
            <Fill><CssParameter name="fill">#84cc16</CssParameter><CssParameter name="fill-opacity">0.85</CssParameter></Fill>
            <Stroke><CssParameter name="stroke">#374151</CssParameter><CssParameter name="stroke-width">0.2</CssParameter></Stroke>
          </PolygonSymbolizer>
        </Rule>
        <Rule>
          <Name>10-15 min</Name>
          <Title>10-15 min</Title>
          <ogc:Filter><ogc:PropertyIsEqualTo><ogc:PropertyName>all_band4p8</ogc:PropertyName><ogc:Literal>10-15 min</ogc:Literal></ogc:PropertyIsEqualTo></ogc:Filter>
          <PolygonSymbolizer>
            <Fill><CssParameter name="fill">#facc15</CssParameter><CssParameter name="fill-opacity">0.85</CssParameter></Fill>
            <Stroke><CssParameter name="stroke">#374151</CssParameter><CssParameter name="stroke-width">0.2</CssParameter></Stroke>
          </PolygonSymbolizer>
        </Rule>
        <Rule>
          <Name>&gt;15 min</Name>
          <Title>&gt;15 min</Title>
          <ogc:Filter><ogc:PropertyIsEqualTo><ogc:PropertyName>all_band4p8</ogc:PropertyName><ogc:Literal>&gt;15 min</ogc:Literal></ogc:PropertyIsEqualTo></ogc:Filter>
          <PolygonSymbolizer>
            <Fill><CssParameter name="fill">#dc2626</CssParameter><CssParameter name="fill-opacity">0.85</CssParameter></Fill>
            <Stroke><CssParameter name="stroke">#374151</CssParameter><CssParameter name="stroke-width">0.2</CssParameter></Stroke>
          </PolygonSymbolizer>
        </Rule>
        <Rule>
          <Name>unreachable</Name>
          <Title>unreachable</Title>
          <ogc:Filter><ogc:PropertyIsEqualTo><ogc:PropertyName>all_band4p8</ogc:PropertyName><ogc:Literal>unreachable</ogc:Literal></ogc:PropertyIsEqualTo></ogc:Filter>
          <PolygonSymbolizer>
            <Fill><CssParameter name="fill">#6b7280</CssParameter><CssParameter name="fill-opacity">0.85</CssParameter></Fill>
            <Stroke><CssParameter name="stroke">#374151</CssParameter><CssParameter name="stroke-width">0.2</CssParameter></Stroke>
          </PolygonSymbolizer>
        </Rule>
      </FeatureTypeStyle>
    </UserStyle>
  </NamedLayer>
</StyledLayerDescriptor>
